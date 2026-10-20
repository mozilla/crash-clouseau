# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Tests for per-signature SuperSearch fallback."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

import requests  # noqa: E402

from crashclouseau import datacollector as dc  # noqa: E402

BID = datetime(2026, 10, 18, 9, 43, 10, tzinfo=timezone.utc)
REFUSED = ("mozilla::net::DocumentLoadListener::OnStatus::<T>::~`lambda at "
           ".\\..\\..\\..\\..\\checkouts\\gecko\\netwerk\\ipc\\DocumentLoadListener.cpp:3658:9'")
GOOD = ["memset | mozilla::MaybePoison", "mozilla::BufferList<T>::AllocateBytes",
        "mozilla::MonitorAutoLockBase<T>::MonitorAutoLockBase",
        "mozilla::dom::FragmentOrElement::DestroyContent", "js::Interpret", "nsAtom::IsStatic"]


def _error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError("{} Client Error".format(status), response=response)


def _fake_search(respond, calls):
    """Defer unfinished callbacks until drained or the next search starts."""
    pending = []

    class Future:
        def __init__(self, query):
            self.query, self.done = query, False

        def result(self):
            if not self.done:
                self.done = True
                pending.remove(self)
                q = self.query
                sgns = q.params["signature"]
                sgns = sgns if isinstance(sgns, list) else [sgns]
                calls.append(sgns)
                if any(REFUSED in s for s in sgns):
                    self.error = respond
                else:
                    self.error = None
                    q.handler(FakeSearch.answer(q.params, [s[1:] for s in sgns]),
                              q.handlerdata)
            if self.error is not None:
                raise self.error

    class FakeSearch:
        URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

        def __init__(self, queries=None, **kwargs):
            for late in list(pending):
                try:
                    late.result()
                except requests.RequestException:
                    pass
            self.results = [Future(q) for q in queries]
            pending.extend(self.results)

        def wait(self):
            for future in self.results:
                future.result()

        @staticmethod
        def answer(params, sgns):
            if "_columns" in params:
                return {"hits": [{"uuid": "u-" + sgns[0], "java_stack_trace": "at a.B.c(B.kt:1)"}],
                        "facets": {"cardinality_install_time": {"value": 2}}}
            if isinstance(params["signature"], str):
                return {"facets": {"cardinality_install_time": {"value": 2}, "proto_signature": [
                    {"term": "p-" + sgns[0], "count": 1,
                     "facets": {"uuid": [{"term": "u-" + sgns[0]}]}}]}}
            return {"facets": {
                "proto_signature": [{"term": "p-" + s, "count": 1, "facets": {
                    "signature": [{"term": s}], "uuid": [{"term": "u-" + s}]}} for s in sgns],
                "signature": [{"term": s, "facets": {"cardinality_install_time": {"value": 2}}}
                              for s in sgns]}}

    return FakeSearch


def _signatures(names):
    return {s: {"bids": {BID: 3}, "protos": {BID: []}, "installs": {BID: 0}} for s in names}


class TestARefusedSignature(unittest.TestCase):
    def _run(self, fetch, names, error=None):
        calls = []
        signatures = _signatures(names)
        fake = _fake_search(error or _error(406), calls)
        with mock.patch.object(dc.socorro, "SuperSearch", fake), \
                mock.patch.object(dc.config, "get_threshold", return_value=5), \
                mock.patch.object(dc.config, "get_limit_facets", return_value=10000):
            fetch(signatures)
        return signatures, calls

    def test_small_keeps_the_signatures_batched_with_it(self):
        signatures, calls = self._run(
            lambda s: dc.get_proto_small("Firefox", s, ">=2026-09-29", "nightly"),
            GOOD[:4] + [REFUSED] + GOOD[4:])
        self.assertEqual(set(signatures), set(GOOD))
        for sgn in GOOD:
            self.assertEqual(signatures[sgn]["protos"][BID],
                             [{"proto": "p-" + sgn, "count": 1, "uuid": "u-" + sgn}])
            self.assertEqual(signatures[sgn]["installs"][BID], 2)
        self.assertIn(["=" + REFUSED], calls)

    def test_big_and_java_drop_only_the_refused_signature(self):
        # Drain GOOD[1] before resetting to avoid duplicate clusters.
        for fetch in (lambda s: dc.get_proto_big("Firefox", s, ">=2026-09-29", "nightly"),
                      lambda s: dc.get_uuids_java("Fenix", s, ">=2026-09-29", "nightly", 1)):
            signatures, _ = self._run(fetch, [GOOD[0], REFUSED, GOOD[1]])
            self.assertEqual(set(signatures), {GOOD[0], GOOD[1]})
            for sgn in (GOOD[0], GOOD[1]):
                self.assertEqual(len(signatures[sgn]["protos"][BID]), 1)

    def test_a_414_is_refused_too(self):
        signatures, _ = self._run(
            lambda s: dc.get_proto_small("Firefox", s, ">=2026-09-29", "nightly"),
            [GOOD[0], REFUSED], error=_error(414))
        self.assertEqual(set(signatures), {GOOD[0]})

    def test_an_outage_still_raises(self):
        for error in (_error(401), _error(403), _error(400), _error(503), _error(429),
                      requests.ConnectionError("down")):
            with self.assertRaises(requests.RequestException):
                self._run(lambda s: dc.get_proto_small("Firefox", s, ">=2026-09-29", "nightly"),
                          [GOOD[0], REFUSED], error=error)


if __name__ == "__main__":
    unittest.main()
