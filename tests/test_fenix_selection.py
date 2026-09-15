# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Turning selected signatures into uuids, honestly: EMPTY, Java and proto-less signatures.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_fenix_selection

Fenix nightly is 54.8% Java/Kotlin reports, 24.7% native and 20.5% `EMPTY: no frame data
available` (plans/16 §5), and the selector used to treat all three alike: a spiking signature was
logged `selected` and handed to the proto-signature facet, which only native reports have. A Java
or EMPTY pair therefore left a `selected` row (`ever_selected`, offered to the spike sweep), a
Stats row with installs=0 and no uuid -- an analysis that never happened. What these tests pin
(plans/16 §13, D10/D11): an EMPTY signature is declined `no_stack` before the spike test, on every
product; a Java signature is read from the `java_stack_trace` column, clustered by a LINE-FREE
shape (R8 remaps the lines), under the cap its pick kind gives a native one; a kept pair Socorro
holds no report for is rewritten `no_protos` and never reaches `put_crashes`; `get_changeset`
speaks the git-shaped frame URI; and the daily rollup pairs Fenix with nightly only.
"""
import contextlib
import inspect
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import config, datacollector as dc, inspector  # noqa: E402
from crashclouseau import models, sigtrend, utils  # noqa: E402

BIDS = ["20260909040000", "20260910040000", "20260911040000", "20260912040000"]
WHEN = datetime(2026, 9, 13)
NEWEST = utils.get_build_date(BIDS[-1])
NATIVE = "mozilla::dom::quota::QuotaManager::Shutdown"
OTHER = "mozilla::places::History::History"
JAVA = "java.lang.OutOfMemoryError: at java.util.Arrays.copyOf(Arrays.java)"
JAVA_RISING = ("javax.crypto.IllegalBlockSizeException: at android.security.keystore2."
               "AndroidKeyStoreCipherSpiBase.engineDoFinal(AndroidKeyStoreCipherSpiBase.java)")
EMPTY = "EMPTY: no frame data available; MissingThreadList"
EMPTY_DESKTOP = "EMPTY: no frame data available; EmptyMinidump"

# Two live Fenix nightly traces (2026-09-15, builds 20260908042139 / 20260912211859), cut to
# the frames that matter. `(pkg.Class.method, File, line)`; line None = "(Native Method)"-style.
_KEYSTORE = [
    ("android.security.keystore2.AndroidKeyStoreCipherSpiBase.engineDoFinal",
     "AndroidKeyStoreCipherSpiBase.java", 613),
    ("javax.crypto.Cipher.doFinal", "Cipher.java", 2074),
    ("mozilla.components.lib.dataprotect.SecurePreferencesImpl23.putString",
     "SecureAbove22Preferences.kt", 145),
    ("mozilla.components.lib.dataprotect.SecureAbove22Preferences.putString",
     "SecureAbove22Preferences.kt", 6),
    ("mozilla.components.service.fxa.SecureAbove22AccountStorage.write", "AccountStorage.kt", 8),
    ("mozilla.components.service.fxa.FirefoxAccount$WrappingPersistenceCallback.persist",
     "FirefoxAccount.kt", 71),
    ("mozilla.appservices.fxaclient.FxaClient.tryPersistState", "FxaClient.kt", 9),
    ("mozilla.appservices.fxaclient.FxaClient.getProfile", "FxaClient.kt", 7),
    ("mozilla.components.service.fxa.FirefoxAccount$getProfile$2$2.invokeSuspend",
     "FirefoxAccount.kt", 10),
    ("kotlinx.coroutines.DispatchedTask.run", "DispatchedTask.kt", 120),
]
_OOM = [
    ("java.util.Arrays.copyOf", "Arrays.java", 4276),
    ("java.io.ByteArrayOutputStream.grow", "ByteArrayOutputStream.java", 120),
    ("kotlin.io.ByteStreamsKt.copyTo", "IOStreams.kt", 17),
    ("mozilla.appservices.viaduct.FetchBackend.sendRequest$lambda$3", "FetchBackend.kt", 4),
    ("mozilla.appservices.viaduct.FetchBackend.sendRequest", "FetchBackend.kt", 273),
    ("kotlinx.coroutines.DispatchedTask.run", "DispatchedTask.kt", 120),
]
_FRAMEWORK_ONLY = [
    ("android.app.servertransaction.PendingTransactionActions$StopInfo.run",
     "PendingTransactionActions.java", 245),
    ("android.os.Handler.handleCallback", "Handler.java", 942),
    ("android.os.Handler.dispatchMessage", "Handler.java", 99),
    ("android.os.Looper.loopOnce", "Looper.java", 201),
]


def _trace(exception, frames):
    """A Socorro ``java_stack_trace``: the exception line, then tab-indented ``at`` frames."""
    lines = [exception]
    for method, filename, line in frames:
        lines.append("\tat {}({}{})".format(method, filename, ":%d" % line if line else ""))
    return "\n".join(lines) + "\n"


def _relined(frames, shift):
    """The same frames with every line number moved -- what R8 does between two builds."""
    return [(m, f, (ln + shift) if ln else ln) for m, f, ln in frames]


def _fake_search(respond):
    """``socorro.SuperSearch`` stand-in for both call shapes the module uses -- ``params=``
    (one query) and ``queries=[Query(...)]`` -- answering each with ``respond(params)``."""
    class FakeSearch:
        URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

        def __init__(self, params=None, handler=None, handlerdata=None, queries=None):
            self._calls = [(q.params, q.handler, q.handlerdata) for q in (queries or [])]
            if params is not None:
                self._calls.append((params, handler, handlerdata))

        def wait(self):
            for params, handler, handlerdata in self._calls:
                handler(respond(params), handlerdata)

    return FakeSearch


def _signature_facets(population, bid):
    """The per-build ``_aggs.signature`` facet `get_new_signatures` reads."""
    return {"errors": None, "facets": {"signature": [
        {"term": sgn, "facets": {"cardinality_install_time": {"value": installs},
                                 "build_id": [{"term": bid, "count": count}]}}
        for sgn, (count, installs) in population.get(bid, {}).items()]}}


def _select(population, product="Fenix", rising=None, respond_more=None):
    """Drive the real ``get_new_signatures`` over ``{buildid: {signature: (count, installs)}}``
    with every fetcher mocked, unless ``respond_more(params)`` answers a fetcher's query (then
    that fetcher runs for real). Returns ``(data, selection, mocks)``."""
    def respond(params):
        if "_aggs.proto_signature" in params or "_columns" in params:
            return respond_more(params)
        return _signature_facets(population, params["build_id"])

    mocks = SimpleNamespace(
        small=mock.MagicMock(), big=mock.MagicMock(), java=mock.MagicMock(),
        rising=mock.Mock(return_value=rising or {}),
    )
    patches = [
        mock.patch.object(dc.socorro, "SuperSearch", _fake_search(respond)),
        mock.patch.object(dc, "get_proto_big", mocks.big),
        mock.patch.object(dc, "_rising_picks", mocks.rising),
        mock.patch.object(dc, "get_builds", return_value=(list(BIDS), ">=2026-09-01")),
    ]
    if respond_more is None:
        patches.append(mock.patch.object(dc, "get_proto_small", mocks.small))
        patches.append(mock.patch.object(dc, "get_uuids_java", mocks.java))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        data, selection = dc.get_new_signatures(product, "nightly", WHEN)
    return data, selection, mocks


def _spiking(*signatures, background=OTHER):
    """Quiet window, then every signature appears on the newest build from a full zero baseline
    (12 reports from 10 installations); `background` keeps the earlier build-days populated."""
    population = {bid: {background: (2, 2)} for bid in BIDS[:3]}
    population[BIDS[3]] = {sgn: (12, 10) for sgn in signatures}
    return population


def _records(selection, signature):
    return [r for r in selection if r["signature"] == signature]


class TestEmptySignaturesAreDeclinedBeforeTheSpikeTest(unittest.TestCase):
    def test_no_stack_is_recorded_and_no_fetcher_ever_sees_it(self):
        data, selection, mocks = _select(_spiking(NATIVE, EMPTY))
        self.assertIn(NATIVE, data)
        self.assertNotIn(EMPTY, data)
        mine = _records(selection, EMPTY)
        self.assertEqual({r["outcome"] for r in mine}, {utils.NO_STACK})
        # One row per build-day it was reported on, never evaluated, nothing picked.
        self.assertEqual(len(mine), 1)
        self.assertTrue(all(r["picked"] is None and not r["evaluable"] for r in mine))
        self.assertEqual(mine[0]["count"], 12)
        # The native signature's own decision is untouched.
        self.assertIn(utils.SELECTED, {r["outcome"] for r in _records(selection, NATIVE)})
        # The fetchers only ever see the native one; the rate path is told to keep off EMPTY.
        self.assertEqual(set(mocks.small.call_args.args[1]), {NATIVE})
        mocks.java.assert_not_called()
        mocks.big.assert_not_called()
        self.assertIn(EMPTY, mocks.rising.call_args.args[4])

    def test_it_applies_to_desktop_too(self):
        """Not conditioned on the product: desktop's rare EmptyMinidump spikes were writing the
        same installs=0 Stats row with no uuid, and the honest record costs nothing."""
        data, selection, _ = _select(_spiking(NATIVE, EMPTY_DESKTOP), product="Firefox")
        self.assertNotIn(EMPTY_DESKTOP, data)
        self.assertEqual({r["outcome"] for r in _records(selection, EMPTY_DESKTOP)},
                         {utils.NO_STACK})

    def test_the_row_fits_the_selection_table_and_is_not_a_selection(self):
        numbers = {datetime(2026, 9, 12): {"count": 12, "bids": {NEWEST: 12},
                                           "installs": {NEWEST: 10}}}
        rec = dict(dc.no_stack_day_records(numbers)[0], signature=EMPTY)
        row = models.Selection._row(rec, "Fenix", "nightly", datetime.now(timezone.utc))
        self.assertEqual((row["outcome"], row["picked"], row["number"], row["ever_selected"]),
                         (utils.NO_STACK, None, 12, False))
        width = models.Selection.__table__.c.outcome.type.length
        for outcome in (utils.NO_STACK, utils.NO_PROTOS):
            self.assertIn(outcome, models.SELECTION_OUTCOMES)
            self.assertNotIn(outcome, models.SELECTED_OUTCOMES)
            self.assertLessEqual(len(outcome), width)


class TestJavaSignaturesTakeTheColumnPath(unittest.TestCase):
    def test_a_java_spike_goes_to_get_uuids_java_under_the_protos_cap(self):
        data, selection, mocks = _select(_spiking(NATIVE, JAVA))
        self.assertEqual(set(data), {NATIVE, JAVA})
        self.assertEqual({r["outcome"] for r in _records(selection, JAVA)}, {utils.SELECTED})
        # Split by class: the native fetcher never sees the Java signature and vice versa.
        self.assertEqual(set(mocks.small.call_args.args[1]), {NATIVE})
        mocks.java.assert_called_once()
        product, signatures, search_date, channel, cap = mocks.java.call_args.args
        self.assertEqual((product, set(signatures), channel), ("Fenix", {JAVA}, "nightly"))
        self.assertEqual(cap, config.get_threshold("protos", "Fenix", "nightly"))
        self.assertEqual(signatures[JAVA]["bids"], {NEWEST: 12})

    def test_a_rising_java_pick_uses_the_rate_paths_own_cap(self):
        pick = {JAVA_RISING: {"bids": {NEWEST: 3}, "protos": {NEWEST: []},
                              "installs": {NEWEST: 0}}}
        data, _, mocks = _select(_spiking(NATIVE), rising=pick)
        self.assertEqual(set(data), {NATIVE, JAVA_RISING})
        # One call per pick kind, each with its own cap, mirroring the native path.
        caps = {frozenset(c.args[1]): c.args[4] for c in mocks.java.call_args_list}
        self.assertEqual(caps, {frozenset({JAVA_RISING}):
                                config.get_spike("rising_protos", "Fenix", "nightly")})
        for call in mocks.small.call_args_list:
            self.assertNotIn(JAVA_RISING, call.args[1])

    def test_the_fennec_path_is_gone(self):
        self.assertFalse(hasattr(dc, "get_uuids_fennec"))
        self.assertNotIn('product == "Fennec"', inspect.getsource(dc))


class TestThePseudoProtoIsLineFree(unittest.TestCase):
    def test_two_traces_differing_only_in_line_numbers_are_one_shape(self):
        a = dc.java_proto(_trace("javax.crypto.IllegalBlockSizeException", _KEYSTORE))
        b = dc.java_proto(_trace("javax.crypto.IllegalBlockSizeException",
                                 _relined(_KEYSTORE, 37)))
        self.assertEqual(a, b)
        self.assertEqual(utils.hash(a), utils.hash(b))
        self.assertTrue(a.startswith("java | "))
        self.assertNotRegex(a, r":\d")

    def test_our_frames_only_capped_at_six(self):
        proto = dc.java_proto(_trace("javax.crypto.IllegalBlockSizeException", _KEYSTORE))
        frames = proto.split(" | ")[1:]
        self.assertEqual(len(frames), dc.JAVA_PROTO_FRAMES)
        self.assertTrue(all(config.is_java_package(f) for f in frames), frames)
        self.assertEqual(frames[0],
                         "mozilla.components.lib.dataprotect.SecurePreferencesImpl23.putString"
                         "(SecureAbove22Preferences.kt)")
        # Two exception sites of one signature are two shapes.
        self.assertNotEqual(proto, dc.java_proto(_trace("java.lang.OutOfMemoryError", _OOM)))

    def test_no_frame_of_ours_falls_back_to_the_first_three_of_anyone(self):
        proto = dc.java_proto(_trace("java.lang.RuntimeException", _FRAMEWORK_ONLY))
        self.assertEqual(proto.split(" | ")[1:], [
            "android.app.servertransaction.PendingTransactionActions$StopInfo.run"
            "(PendingTransactionActions.java)",
            "android.os.Handler.handleCallback(Handler.java)",
            "android.os.Handler.dispatchMessage(Handler.java)",
        ])

    def test_r8_synthetic_and_native_method_frames_parse(self):
        trace = _trace("java.lang.ArithmeticException", [
            ("org.mozilla.fenix.debugsettings.crashtools.CrashToolsKt$$ExternalSyntheticLambda4"
             ".invoke", "R8$$SyntheticClass", 5),
            ("android.database.sqlite.SQLiteConnection.nativePrepareStatement", "Native Method",
             None),
        ]) + "\tat android.app.\n"          # Socorro truncates the column mid-frame
        self.assertEqual(dc.java_proto(trace), (
            "java | org.mozilla.fenix.debugsettings.crashtools.CrashToolsKt"
            "$$ExternalSyntheticLambda4.invoke(R8$$SyntheticClass)"))

    def test_nothing_parses_to_nothing(self):
        self.assertEqual(dc.java_proto(None), "")
        self.assertEqual(dc.java_proto(""), "")
        self.assertEqual(dc.java_proto("java.lang.OutOfMemoryError\n"), "")


def _hits(*traces):
    return [{"uuid": "u-%d" % i, "java_stack_trace": t} for i, t in enumerate(traces)]


class TestGetUuidsJava(unittest.TestCase):
    EXC = "javax.crypto.IllegalBlockSizeException"

    def _fetch(self, hits, cap, installs=3, bids=(NEWEST,)):
        seen = []

        def respond(params):
            seen.append(params)
            return {"errors": [], "hits": hits,
                    "facets": {"cardinality_install_time": {"value": installs}}}

        signatures = {JAVA_RISING: {"bids": {b: len(hits) for b in bids},
                                    "protos": {b: [] for b in bids},
                                    "installs": {b: 0 for b in bids}}}
        with mock.patch.object(dc.socorro, "SuperSearch", _fake_search(respond)):
            dc.get_uuids_java("Fenix", signatures, ">=2026-09-01", "nightly", cap)
        return signatures, seen

    def test_one_query_per_pair_with_the_column_read(self):
        _, seen = self._fetch(_hits(_trace(self.EXC, _KEYSTORE)), cap=5)
        self.assertEqual(len(seen), 1)
        params = seen[0]
        self.assertEqual(params["product"], "Fenix")
        self.assertEqual(params["release_channel"], "nightly")
        self.assertEqual(params["build_id"], BIDS[-1])
        self.assertEqual(params["signature"], "=" + JAVA_RISING)
        self.assertEqual(params["date"], ">=2026-09-01")
        self.assertEqual(params["_columns"], ["uuid", "java_stack_trace"])
        self.assertGreaterEqual(params["_results_number"], 5)
        self.assertEqual(params["_facets"], "_cardinality.install_time")

    def test_hits_are_clustered_by_shape_loudest_first_and_capped(self):
        hits = _hits(
            _trace(self.EXC, _KEYSTORE),                 # shape A
            _trace(self.EXC, _relined(_KEYSTORE, 3)),    # shape A, other lines
            _trace("java.lang.OutOfMemoryError", _OOM),  # shape B
            _trace(self.EXC, _relined(_KEYSTORE, 9)),    # shape A again
            None,                                        # no column: skipped
        )
        signatures, _ = self._fetch(hits, cap=5)
        protos = signatures[JAVA_RISING]["protos"][NEWEST]
        self.assertEqual([(p["count"], p["uuid"]) for p in protos], [(3, "u-0"), (1, "u-2")])
        self.assertEqual(protos[0]["proto"], dc.java_proto(hits[0]["java_stack_trace"]))
        self.assertEqual(signatures[JAVA_RISING]["installs"][NEWEST], 3)
        # The cap is the number of SHAPES, and the loudest survive it.
        signatures, _ = self._fetch(hits, cap=1)
        self.assertEqual([p["uuid"] for p in signatures[JAVA_RISING]["protos"][NEWEST]], ["u-0"])

    def test_zero_installations_is_coerced_to_one_like_the_native_path(self):
        signatures, _ = self._fetch(_hits(_trace(self.EXC, _KEYSTORE)), cap=5, installs=0)
        self.assertEqual(signatures[JAVA_RISING]["installs"][NEWEST], 1)

    def test_identical_shapes_on_two_builds_share_one_cluster(self):
        previous = utils.get_build_date(BIDS[-2])
        signatures, seen = self._fetch(_hits(_trace(self.EXC, _KEYSTORE)), cap=5,
                                       bids=(previous, NEWEST))
        self.assertEqual(len(seen), 2)
        protos = signatures[JAVA_RISING]["protos"]
        self.assertEqual(utils.hash(protos[previous][0]["proto"]),
                         utils.hash(protos[NEWEST][0]["proto"]))

    def test_a_pair_with_no_readable_trace_is_removed(self):
        signatures, _ = self._fetch(_hits(None, "java.lang.OutOfMemoryError\n"), cap=5)
        self.assertEqual(signatures, {})

    def test_a_socorro_error_yields_nothing_rather_than_a_crash(self):
        def respond(params):
            return {"errors": ["boom"], "hits": [], "facets": {}}

        signatures = {JAVA: {"bids": {NEWEST: 3}, "protos": {NEWEST: []}, "installs": {NEWEST: 0}}}
        with mock.patch.object(dc.socorro, "SuperSearch", _fake_search(respond)):
            dc.get_uuids_java("Fenix", signatures, ">=2026-09-01", "nightly", 5)
        self.assertEqual(signatures, {})


class TestAKeptPairWithNoReportIsRewritten(unittest.TestCase):
    def _proto_small_response(self, params):
        """`get_proto_small`'s facets: OTHER has one proto cluster, NATIVE none at all."""
        self.assertIn("_aggs.proto_signature", params)
        self.assertEqual(set(params["signature"]), {"=" + NATIVE, "=" + OTHER})
        return {"errors": [], "facets": {
            "proto_signature": [{"term": "OtherProto", "count": 3, "facets": {
                "signature": [{"term": OTHER}], "uuid": [{"term": "u-other"}]}}],
            "signature": [{"term": OTHER,
                           "facets": {"cardinality_install_time": {"value": 4}}}],
        }}

    def test_no_protos_replaces_selected_and_the_pair_leaves_data(self):
        data, selection, _ = _select(_spiking(NATIVE, OTHER, background=JAVA_RISING),
                                     respond_more=self._proto_small_response)
        self.assertEqual(set(data), {OTHER})
        self.assertEqual(data[OTHER]["protos"][NEWEST],
                         [{"proto": "OtherProto", "count": 3, "uuid": "u-other"}])
        self.assertEqual(data[OTHER]["installs"][NEWEST], 4)
        mine = _records(selection, NATIVE)
        self.assertEqual({r["outcome"] for r in mine}, {utils.NO_PROTOS})
        self.assertTrue(all(r["picked"] is None for r in mine))
        # The day is still described as what it was: an evaluable build-day that spiked.
        self.assertTrue(all(r["spiked"] and r["evaluable"] for r in mine))
        self.assertEqual({r["outcome"] for r in _records(selection, OTHER)}, {utils.SELECTED})

    def test_the_rewritten_row_never_claims_an_analysis(self):
        rec = {"signature": NATIVE, "day": datetime(2026, 9, 12), "count": 12, "index": 3,
               "baseline": [0, 0, 0], "evaluable": True, "spiked": True,
               "bids": {NEWEST: 12}, "installs": {NEWEST: 10}, "picked": NEWEST,
               "outcome": utils.SELECTED}
        untouched = dict(rec, outcome=utils.NOT_SPIKING, picked=None, day=datetime(2026, 9, 11))
        selection = [rec, untouched]
        self.assertEqual(dc.declare_no_protos({NATIVE}, selection, "Fenix", "nightly"), 1)
        row = models.Selection._row(rec, "Fenix", "nightly", datetime.now(timezone.utc))
        self.assertEqual((row["outcome"], row["picked"], row["ever_selected"]),
                         (utils.NO_PROTOS, None, False))
        self.assertEqual(untouched["outcome"], utils.NOT_SPIKING)

    def test_drop_unresolved_removes_only_the_pairs_with_nothing_on_any_build(self):
        previous = utils.get_build_date(BIDS[-2])
        signatures = {
            NATIVE: {"bids": {previous: 2, NEWEST: 12}, "protos": {previous: [], NEWEST: []},
                     "installs": {previous: 0, NEWEST: 0}},
            OTHER: {"bids": {previous: 2, NEWEST: 12},
                    "protos": {previous: [{"proto": "p", "count": 1, "uuid": "u"}], NEWEST: []},
                    "installs": {previous: 1, NEWEST: 0}},
        }
        self.assertEqual(dc.drop_unresolved(signatures, "Fenix", "nightly", "proto-signature"),
                         {NATIVE})
        self.assertEqual(set(signatures), {OTHER})

    def test_nothing_to_declare_touches_nothing(self):
        selection = [{"signature": NATIVE, "outcome": utils.SELECTED, "picked": NEWEST}]
        self.assertEqual(dc.declare_no_protos(set(), selection, "Fenix", "nightly"), 0)
        self.assertEqual(selection[0]["outcome"], utils.SELECTED)


class TestGetChangesetSpeaksGit(unittest.TestCase):
    """Live 2026-09-15: Firefox nightly 20260912093409 voted 240 reports on one sha, Fenix
    nightly 20260912211859 33 on another; the hg-shaped filter matched 0 on both."""

    SHA = "251c0992cd1f4204e3ea188ff00c4dbf845fb077"
    OLD = "39dcc5beb77ec138d9a3f3418fea1159efe54e2e"
    HG = "45306dee8419383e2ce5c1619f3fbc613ec345f8"

    def _vote(self, terms, hg):
        seen = []

        def respond(params):
            seen.append(params)
            facets = {"build_id": [{"term": BIDS[-1], "count": sum(c for c, _ in terms),
                                    "facets": {"topmost_filenames": [
                                        {"term": t, "count": c} for c, t in terms]}}]}
            return {"errors": [], "facets": facets if terms else {"build_id": []}}

        with mock.patch.object(dc.socorro, "SuperSearch", _fake_search(respond)), \
                mock.patch.object(inspector, "git2hg", return_value=hg) as git2hg:
            rev = dc.get_changeset(NEWEST, "nightly", "Fenix")
        return rev, seen[0], git2hg

    def _uri(self, path, sha):
        return "git:github.com/mozilla-firefox/firefox:{}:{}".format(path, sha)

    def test_the_winning_sha_goes_through_lando(self):
        terms = [(135, self._uri("mfbt/assertions.h", self.SHA)),
                 (20, self._uri("xpcom/base/nsdebugimpl.cpp", self.SHA)),
                 (3, self._uri("mfbt/refptr.h", self.OLD))]
        rev, params, git2hg = self._vote(terms, self.HG)
        self.assertEqual(rev, self.HG[:12])
        git2hg.assert_called_once_with(self.SHA)
        self.assertEqual(params["topmost_filenames"], dc._GIT_TOPMOST_FILENAMES)
        self.assertEqual((params["product"], params["release_channel"], params["build_id"]),
                         ("Fenix", "nightly", BIDS[-1]))

    def test_the_filter_is_the_uri_form_the_frame_parser_reads(self):
        """One shape, two readers: the Socorro filter here and `inspector.GIT_PAT` on frames.
        Live URIs have no slash after the repository; a filter with one matched nothing."""
        uri = self._uri("mfbt/assertions.h", self.SHA)
        self.assertEqual(inspector.GIT_PAT.match(uri).groups(), ("mfbt/assertions.h", self.SHA))
        prefix = dc._GIT_TOPMOST_FILENAMES.split('"')[1]
        self.assertTrue(uri.startswith(prefix), prefix)
        self.assertTrue(prefix.endswith("firefox:"))

    def test_a_sha_lando_does_not_map_is_none(self):
        rev, _, _ = self._vote([(5, self._uri("mfbt/refptr.h", self.OLD))], "")
        self.assertIsNone(rev)

    def test_no_vote_is_none_without_asking_lando(self):
        rev, _, git2hg = self._vote([], self.HG)
        self.assertIsNone(rev)
        git2hg.assert_not_called()


class TestCollectAllPairsFenixWithNightlyOnly(unittest.TestCase):
    def _run(self, **kw):
        backfill = mock.Mock(return_value=1)
        with mock.patch.object(sigtrend, "backfill", backfill), \
                mock.patch.object(sigtrend.models.SignatureDaily, "prune"), \
                mock.patch.object(sigtrend.models.ChannelDaily, "prune"), \
                mock.patch.object(sigtrend.config, "get_ingest_products",
                                  return_value=["Firefox", "Fenix"]), \
                mock.patch.object(sigtrend.config, "get_channels",
                                  return_value=["nightly", "beta", "release", "esr153"]):
            total = sigtrend.collect_all(**kw)
        return total, [c.args[:2] for c in backfill.call_args_list]

    def test_the_shipped_pairing(self):
        self.assertEqual(config.get_product_channels("Fenix"), ["nightly"])
        self.assertEqual(config.get_product_channels("Firefox"), config.get_channels())

    def test_fenix_is_rolled_up_on_nightly_and_nowhere_else(self):
        total, pairs = self._run()
        self.assertEqual(pairs, [("Firefox", "nightly"), ("Firefox", "beta"),
                                 ("Firefox", "release"), ("Firefox", "esr153"),
                                 ("Fenix", "nightly")])
        self.assertEqual(total, 5)

    def test_an_explicit_pair_outside_the_product_is_refused(self):
        total, pairs = self._run(products=["Fenix"], channels=["beta"])
        self.assertEqual((total, pairs), (0, []))


if __name__ == "__main__":
    unittest.main()
