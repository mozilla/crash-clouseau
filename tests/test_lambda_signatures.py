# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""One lambda, two Socorro signatures -- decided as one.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_lambda_signatures

MSVC demangles a lambda to ``<lambda_1>`` and Socorro collapses it to ``<T>``; clang gives
``$_95`` and Socorro collapses it to ``$``. So ``QuotaManager::Shutdown::<T>::operator()``
(Windows) and ``QuotaManager::Shutdown::$::operator()`` (Linux, macOS) are one frame of one
defect, and every per-signature instrument saw each half alone. The 2026-08-14 nightly build-day
of that signature fires the 3x spike rule merged (27 vs a prior max of 9) and misses it split
(19 vs 21 needed, 8 vs 9 needed). Neither half was ever selected.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import contextlib  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, datacollector as dc, models, utils  # noqa: E402

T = "mozilla::dom::quota::QuotaManager::Shutdown::<T>::operator()"
D = "mozilla::dom::quota::QuotaManager::Shutdown::$::operator()"
HANG = ("shutdownhang | mozilla::SpinEventLoopUntil<T> | "
        "mozilla::dom::quota::QuotaManager::Observer::Observe")


class TestOneLambdaTwoSignatures(unittest.TestCase):
    def test_the_two_demanglings_share_a_family(self):
        self.assertEqual(utils.lambda_family(D), T)
        self.assertEqual(utils.lambda_family(T), T)
        self.assertEqual(utils.lambda_siblings(T), {T, D})
        self.assertEqual(utils.lambda_siblings(D), {T, D})

    def test_the_raw_toolchain_spellings_normalise_too(self):
        for raw in ("Foo::Shutdown::$_95::operator()", "Foo::Shutdown::<lambda_0>::operator()",
                    "Foo::Shutdown::{lambda#1}::operator()"):
            self.assertEqual(utils.lambda_family(raw), "Foo::Shutdown::<T>::operator()", raw)

    def test_a_template_argument_is_not_a_lambda(self):
        # `SpinEventLoopUntil<T>` is a template on a NAME; only a standalone component is a lambda.
        self.assertEqual(utils.lambda_family(HANG), HANG)
        self.assertEqual(utils.lambda_siblings(HANG), {HANG})
        self.assertEqual(utils.lambda_family("mozilla::Foo::Bar"), "mozilla::Foo::Bar")

    def test_the_last_frame_and_a_middle_frame_both_count(self):
        sig = "A::$::__invoke | B::$::operator() | C::Run"
        self.assertEqual(utils.lambda_family(sig),
                         "A::<T>::__invoke | B::<T>::operator() | C::Run")

    def test_families_lists_only_the_split_ones(self):
        fam = utils.lambda_families([T, D, "mozilla::Foo::Bar", HANG])
        self.assertEqual(fam, {T: [D, T], D: [D, T]})

    def test_merge_sums_counts_bids_and_installs(self):
        day = datetime(2026, 8, 14)
        merged = utils.merge_day_series([
            {day: {"count": 19, "bids": {"b": 19}, "installs": {"b": 19}}},
            {day: {"count": 8, "bids": {"b": 8}, "installs": {"b": 8}}},
        ])
        self.assertEqual(merged, {day: {"count": 27, "bids": {"b": 27}, "installs": {"b": 27}}})


# The 2026-08-14 nightly build-day, per Socorro: one build a day, 08-11..08-14, and the two
# demanglings' report counts (installs == reports on this signature, one crash per machine).
BIDS = ["20260811093000", "20260812093000", "20260813093000", "20260814093000"]
_T_COUNTS = [6, 7, 6, 19]
_D_COUNTS = [2, 1, 3, 8]


def population(forms):
    pop = {}
    for bid, t, d in zip(BIDS, _T_COUNTS, _D_COUNTS):
        pop[bid] = {}
        if "T" in forms:
            pop[bid][T] = (t, t)
        if "D" in forms:
            pop[bid][D] = (d, d)
    return pop


def run_selector(pop, bids=BIDS, channel="nightly", when=datetime(2026, 8, 15), extra=()):
    """Drive the real `get_new_signatures` over a synthetic per-build Socorro population.
    Returns ``(data, selection, get_proto_small mock)``."""

    class FakeSuperSearch:
        def __init__(self, params=None, handler=None, handlerdata=None):
            bid = params["build_id"]
            facets = [
                {"term": sgn, "facets": {"cardinality_install_time": {"value": installs},
                                         "build_id": [{"term": bid, "count": count}]}}
                for sgn, (count, installs) in pop.get(bid, {}).items()
            ]
            handler({"errors": None, "facets": {"signature": facets}}, handlerdata)

        def wait(self):
            pass

    proto_small = mock.MagicMock()
    patches = [
        mock.patch.object(dc.socorro, "SuperSearch", FakeSuperSearch),
        mock.patch.object(dc, "get_proto_small", proto_small),
        mock.patch.object(dc, "get_proto_big"),
        mock.patch.object(dc, "get_builds", return_value=(list(bids), ">=2026-08-10")),
    ]
    patches.extend(extra)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        data, selection = dc.get_new_signatures("Firefox", channel, when)
    return data, selection, proto_small


def no_rate_path():
    return (mock.patch.object(dc, "_rising_picks", return_value={}),)


class TestSplitLambdaIsDecidedAsOne(unittest.TestCase):
    def test_either_half_alone_is_not_a_spike(self):
        for forms in ("T", "D"):
            data, selection, _ = run_selector(population(forms), extra=no_rate_path())
            self.assertEqual(data, {}, forms)
            self.assertFalse([r for r in selection if r["outcome"] == utils.SELECTED], forms)

    def test_merged_the_build_day_fires_and_both_halves_are_analysed(self):
        data, selection, _ = run_selector(population("TD"), extra=no_rate_path())
        self.assertEqual(set(data), {T, D})
        spike_bid = utils.get_build_date(BIDS[-1])
        # Each half carries ITS OWN crashes on the picked build, so its own protos get fetched.
        self.assertEqual(data[T]["bids"], {spike_bid: 19})
        self.assertEqual(data[D]["bids"], {spike_bid: 8})
        picked = {r["signature"]: r for r in selection if r["outcome"] == utils.SELECTED}
        self.assertEqual(set(picked), {T, D})
        # The log explains the pick with the FAMILY's numbers and names the other half.
        self.assertEqual(picked[T]["count"], 27)
        self.assertEqual(picked[T]["baseline"], [8, 8, 9])
        self.assertEqual(picked[T]["merged_with"], [D])
        self.assertEqual(picked[D]["merged_with"], [T])

    def test_the_merged_record_survives_the_row_conversion(self):
        _, selection, _ = run_selector(population("TD"), extra=no_rate_path())
        rec = next(r for r in selection if r["outcome"] == utils.SELECTED)
        row = models.Selection._row(rec, "Firefox", "nightly", datetime.now(timezone.utc))
        self.assertTrue(row["ever_selected"])
        self.assertEqual(row["number"], 27)

    def test_an_unrelated_signature_is_decided_alone_as_before(self):
        pop = population("TD")
        for bid, n in zip(BIDS, (0, 0, 0, 4)):
            if n:
                pop[bid]["mozilla::Foo::Bar"] = (n, n)
        data, _, _ = run_selector(pop, extra=no_rate_path())
        self.assertIn("mozilla::Foo::Bar", data)          # from-zero, its own decision


class TestTheFilerSeesBothSpellings(unittest.TestCase):
    def test_the_venue_lookup_asks_bugzilla_for_both(self):
        captured = {}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"bugs": [{"id": 1588498, "summary": "[meta] QM shutdown hangs",
                                  "cf_crash_signature": "[@ " + T + " ]",
                                  "keywords": ["meta"], "product": "Core"}]}

        def get(url, params=None, timeout=None):
            captured.update(params)
            return R()

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=get):
            bugs = bugzilla_apply._open_bugs_for_signature(D)
        values = {v for k, v in captured.items() if k.startswith("v")}
        self.assertIn(T, values)
        self.assertIn(D, values)
        self.assertEqual(captured["j_top"], "OR")
        # A bug carrying only the OTHER spelling is a venue for this crash.
        self.assertEqual([b["id"] for b in bugs], [1588498])

    def test_a_signature_without_a_lambda_asks_as_before(self):
        captured = {}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"bugs": []}

        def get(url, params=None, timeout=None):
            captured.update(params)
            return R()

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=get):
            bugzilla_apply._open_bugs_for_signature("mozilla::Foo::Bar")
        self.assertEqual(captured["v1"], "mozilla::Foo::Bar")
        self.assertEqual(captured["v2"], "[@ mozilla::Foo::Bar")
        self.assertNotIn("v3", captured)


if __name__ == "__main__":
    unittest.main()
