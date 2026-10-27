# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Test filing serialization, lease renewal, and spike retries.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_filing_lock
"""
import contextlib
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import redis  # noqa: E402

from crashclouseau import bugzilla_apply, worker  # noqa: E402
from crashclouseau.agent import spike_agent, spike_escalation as se  # noqa: E402

KEY = bugzilla_apply._FILING_LOCK_KEY


class _ExpiringLock:
    """In-memory lock with expiry and token-checked renewal."""

    def __init__(self, store, name, timeout, sleep, blocking_timeout):
        self.store, self.name, self.timeout = store, name, timeout
        self.sleep, self.blocking_timeout, self.token = sleep, blocking_timeout, None

    def _expiry(self):
        return float("inf") if self.timeout is None else time.monotonic() + self.timeout

    def acquire(self):
        token = object()
        stop_at = (None if self.blocking_timeout is None
                   else time.monotonic() + self.blocking_timeout)
        while True:
            with self.store.guard:
                if self.store.owner_locked(self.name) is None:
                    self.store.keys[self.name] = (token, self._expiry())
                    self.token = token
                    return True
            if stop_at is not None and time.monotonic() + self.sleep > stop_at:
                return False
            time.sleep(self.sleep)

    def _check_owned(self):
        if self.token is None or self.store.owner_locked(self.name) is not self.token:
            raise redis.exceptions.LockNotOwnedError("no longer owned")

    def reacquire(self):
        with self.store.guard:
            self._check_owned()
            self.store.keys[self.name] = (self.token, self._expiry())
        return True

    def release(self):
        with self.store.guard:
            self._check_owned()
            del self.store.keys[self.name]
        self.token = None


class _ExpiringRedis:
    """Record lock requests and model expiring keys."""

    def __init__(self):
        self.keys, self.requests = {}, []
        self.guard = threading.RLock()

    def lock(self, name, timeout=None, sleep=0.1, blocking_timeout=None, thread_local=True):
        with self.guard:
            self.requests.append({"name": name, "timeout": timeout,
                                  "blocking_timeout": blocking_timeout,
                                  "thread_local": thread_local})
        return _ExpiringLock(self, name, timeout, sleep, blocking_timeout)

    def owner_locked(self, name):
        token, expires = self.keys.get(name, (None, 0.0))
        return token if expires > time.monotonic() else None

    def held(self, name=KEY):
        with self.guard:
            return self.owner_locked(name) is not None


def _file(**kw):
    return bugzilla_apply.autofile_bug("u-1", {"signature": "S"}, {}, {}, "actionable", 70, **kw)


class TestTheFilingLock(unittest.TestCase):
    def setUp(self):
        self.conn = _ExpiringRedis()
        p = mock.patch.object(worker, "conn", self.conn)
        p.start()
        self.addCleanup(p.stop)

    def test_a_filing_runs_inside_the_lock(self):
        seen = []

        def body(*a, **k):
            seen.append(self.conn.held())
            return {"filed": True, "bug": 1}

        with mock.patch.object(bugzilla_apply, "_autofile_bug", side_effect=body):
            self.assertEqual(_file(), {"filed": True, "bug": 1})
        self.assertEqual(seen, [True])
        self.assertFalse(self.conn.held())
        self.assertEqual(self.conn.requests, [{"name": KEY,
                                               "timeout": bugzilla_apply._FILING_LOCK_TTL_S,
                                               "blocking_timeout": 300,
                                               "thread_local": False}])

    def test_the_lock_is_released_when_the_filer_raises(self):
        with mock.patch.object(bugzilla_apply, "_autofile_bug", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                _file()
        self.assertFalse(self.conn.held())

    def test_a_dry_run_neither_takes_nor_waits_for_the_lock(self):
        self.conn.lock(KEY).acquire()
        self.conn.requests.clear()
        with mock.patch.object(bugzilla_apply, "_autofile_bug",
                               return_value={"filed": False, "skipped": "x"}):
            res = _file(dry_run=True)
        self.assertEqual(res, {"filed": False, "skipped": "x", "dry_run": True})
        self.assertEqual(self.conn.requests, [])

    def test_a_busy_lock_declines_without_filing(self):
        self.conn.lock(KEY).acquire()
        with mock.patch.object(bugzilla_apply, "_autofile_bug") as body, \
                self.assertLogs(level="WARNING"):
            res = _file(lock_wait=0)
        body.assert_not_called()
        self.assertEqual(res, {"filed": False,
                               "skipped": bugzilla_apply.FILING_LOCK_BUSY.format(0)})

    def test_the_wait_defaults_and_is_never_negative(self):
        # Renewal allows the acquisition wait to exceed the initial TTL.
        self.assertEqual(bugzilla_apply.filing_lock_wait(None), bugzilla_apply._FILING_LOCK_WAIT_S)
        self.assertEqual(bugzilla_apply.filing_lock_wait(2000), 2000)
        self.assertEqual(bugzilla_apply.filing_lock_wait(-5), 0)
        with mock.patch.object(bugzilla_apply, "_autofile_bug", return_value={}):
            _file(lock_wait=2000)
        self.assertEqual(self.conn.requests[-1]["blocking_timeout"], 2000)

    def test_an_unreachable_redis_files_without_the_lock(self):
        broken = mock.Mock()
        broken.lock.return_value.acquire.side_effect = redis.exceptions.ConnectionError("down")
        with mock.patch.object(worker, "conn", broken), \
                mock.patch.object(bugzilla_apply, "_autofile_bug",
                                  return_value={"filed": True, "bug": 1}) as body, \
                self.assertLogs(level="WARNING"):
            self.assertEqual(_file(), {"filed": True, "bug": 1})
        body.assert_called_once()
        broken.lock.return_value.release.assert_not_called()

    def test_a_lock_that_expired_while_held_is_not_an_error(self):
        expired = mock.Mock()
        expired.lock.return_value.acquire.return_value = True
        expired.lock.return_value.release.side_effect = redis.exceptions.LockNotOwnedError("gone")
        with mock.patch.object(worker, "conn", expired), \
                mock.patch.object(bugzilla_apply, "_autofile_bug",
                                  return_value={"filed": True, "bug": 1}), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(_file(), {"filed": True, "bug": 1})


class TestTheLeaseOutlivesItsTtl(unittest.TestCase):
    """Lease renewal prevents expiry during a slow filing."""

    def setUp(self):
        self.conn = _ExpiringRedis()
        for p in (mock.patch.object(worker, "conn", self.conn),
                  mock.patch.object(bugzilla_apply, "_FILING_LOCK_TTL_S", 0.5),
                  mock.patch.object(bugzilla_apply, "_FILING_LOCK_RENEW_S", 0.1)):
            p.start()
            self.addCleanup(p.stop)

    def _contend(self, hold=1.5):
        events, guard = [], threading.Lock()

        def note(what):
            with guard:
                events.append(what)

        def holder():
            with bugzilla_apply.filing_lock(0) as held:
                note(("holder in", held))
                time.sleep(hold)
                note(("holder out", None))

        def contender():
            time.sleep(0.05)
            with bugzilla_apply.filing_lock(5) as held:
                note(("contender in", held))

        threads = [threading.Thread(target=holder), threading.Thread(target=contender)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return events

    def test_a_live_holder_keeps_the_lock_past_the_ttl(self):
        self.assertEqual(self._contend(), [("holder in", True), ("holder out", None),
                                           ("contender in", True)])

    def test_without_renewal_the_lease_lapses_under_a_live_holder(self):
        with mock.patch.object(bugzilla_apply, "_keep_filing_lock", lambda lock, stop: None):
            self.assertEqual(self._contend(), [("holder in", True), ("contender in", True),
                                               ("holder out", None)])

    def test_a_lost_lease_is_logged(self):
        with self.assertLogs(level="ERROR") as logs, bugzilla_apply.filing_lock(0) as held:
            self.assertTrue(held)
            with self.conn.guard:
                del self.conn.keys[KEY]
            time.sleep(0.35)
        self.assertIn("lost the filing lock", "\n".join(logs.output))


class TestConcurrentRunsOfOneSignature(unittest.TestCase):
    """Concurrent duplicate checks must see earlier filings."""

    def _race(self, runs=4):
        filed, results = [], []
        guard = threading.Lock()

        def body(uuid, *a, **k):
            with guard:
                prior = list(filed)
            if prior:
                return {"filed": False, "skipped": "already filed bug {}".format(prior[0])}
            time.sleep(0.2)  # Allow other runs to read before this run records a filing.
            with guard:
                filed.append(2086239 + len(filed))
                return {"filed": True, "bug": filed[-1]}

        def run(i):
            res = bugzilla_apply.autofile_bug("u-{}".format(i), {}, {}, {}, "actionable", 70)
            with guard:
                results.append(res)

        with mock.patch.object(bugzilla_apply, "_autofile_bug", side_effect=body):
            threads = [threading.Thread(target=run, args=(i,)) for i in range(runs)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        return filed, results

    def test_without_the_lock_every_run_files(self):
        @contextlib.contextmanager
        def no_lock(wait=None):
            yield True

        with mock.patch.object(bugzilla_apply, "filing_lock", no_lock):
            filed, _ = self._race()
        self.assertEqual(len(filed), 4)

    def test_with_the_lock_one_run_files_and_the_others_see_it(self):
        with mock.patch.object(worker, "conn", _ExpiringRedis()):
            filed, results = self._race()
        self.assertEqual(filed, [2086239])
        self.assertEqual(sorted(r.get("skipped") or "" for r in results),
                         ["", "already filed bug 2086239", "already filed bug 2086239",
                          "already filed bug 2086239"])


class _StubLock:
    """Track lock entry and requested waits without blocking."""

    def __init__(self, held):
        self.held, self.inside, self.waits = held, False, []

    @contextlib.contextmanager
    def __call__(self, wait=None):
        self.waits.append(wait)
        self.inside = True
        try:
            yield self.held
        finally:
            self.inside = False


class TestTheSpikeFilerTakesTheLock(unittest.TestCase):
    def _esc(self, **over):
        base = dict(id=7, signature="mozilla::Foo::Bar", product="Firefox", channel="nightly",
                    buildid="20260903093145", uuid="u-1", status="pending", attempts=0,
                    payload={}, merge_payload=mock.Mock(), set_status=mock.Mock(),
                    add_usage=mock.Mock())
        base.update(over)
        return SimpleNamespace(**base)

    def _run(self, esc, lock):
        run = SimpleNamespace(total_cost_usd=0.1, input_tokens=1, output_tokens=1,
                              cache_read_tokens=0, findings=None, provenance=[], result="r",
                              error=None, grounded=True, num_turns=1, tool_calls=0,
                              usage=lambda: {})

        async def agent(brief):
            return run

        with mock.patch.object(se.models.SpikeEscalation, "get", return_value=esc), \
                mock.patch.object(se, "build_spike_brief", return_value={}), \
                mock.patch.object(spike_agent, "run_spike_agent", agent), \
                mock.patch.object(se, "validate_findings", return_value=(None, [])), \
                mock.patch.object(se, "_public_brief", return_value={}), \
                mock.patch.object(se.samesite, "record_for_spike"), \
                mock.patch.object(se.bugzilla_apply, "filing_lock", lock), \
                mock.patch.object(se, "file_spike_bug",
                                  return_value={"filed": True, "bug": 9}) as filer:
            se.run_spike_escalation(7)
        return filer

    def test_a_spike_files_and_records_inside_the_lock(self):
        esc, lock = self._esc(), _StubLock(True)
        recorded = []
        esc.merge_payload.side_effect = lambda values, **k: recorded.append(
            ("filing" in values, lock.inside))
        filer = self._run(esc, lock)
        filer.assert_called_once()
        esc.merge_payload.assert_called_with({"filing": {"filed": True, "bug": 9}})
        # The persistence call must occur before unlocking.
        self.assertIn((True, True), recorded)
        esc.set_status.assert_called_with("done")

    def test_a_busy_lock_leaves_a_retryable_decline(self):
        esc = self._esc()
        filer = self._run(esc, _StubLock(False))
        filer.assert_not_called()
        filing = esc.merge_payload.call_args.args[0]["filing"]
        self.assertEqual((filing["filed"], filing["retry"], filing["uuid"]), (False, True, "u-1"))
        self.assertEqual(filing["skipped"], bugzilla_apply.FILING_LOCK_BUSY.format(300))
        esc.set_status.assert_called_with("done")

    def _retry(self, lock, rows=1):
        self.inside_at_merge = []
        escs = []
        for i in range(rows):
            esc = self._esc(id=7 + i, status="done",
                            updated=datetime.now(timezone.utc) - timedelta(hours=1),
                            payload={"filing": {"filed": False, "retry": True, "attempts": 1}})
            esc.merge_payload.side_effect = lambda values, **k: self.inside_at_merge.append(
                getattr(lock, "inside", None))
            escs.append(esc)
        query = mock.Mock()
        query.filter.return_value.all.return_value = escs
        with mock.patch.object(se.db.session, "query", return_value=query), \
                mock.patch.object(se, "build_spike_brief", return_value={}), \
                mock.patch.object(se.bugzilla_apply, "filing_lock", lock), \
                mock.patch.object(se, "file_spike_bug",
                                  return_value={"filed": True, "bug": 9}) as filer:
            retried = se._retry_filings({"lookback_days": 5})
        return escs[0] if rows == 1 else escs, filer, retried

    def test_a_retry_files_and_records_inside_the_lock(self):
        lock = _StubLock(True)
        esc, filer, retried = self._retry(lock)
        self.assertEqual(retried, 1)
        filer.assert_called_once()
        esc.merge_payload.assert_called_once_with({"filing": {"filed": True, "bug": 9,
                                                              "attempts": 2}})
        self.assertEqual(self.inside_at_merge, [True])

    def test_a_busy_lock_ends_the_pass_without_spending_an_attempt(self):
        # The first acquisition timeout must leave all retry rows untouched.
        lock = _StubLock(False)
        escs, filer, retried = self._retry(lock, rows=3)
        self.assertEqual(retried, 0)
        self.assertEqual(lock.waits, [se._RETRY_LOCK_WAIT_S])
        filer.assert_not_called()
        for esc in escs:
            esc.merge_payload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
