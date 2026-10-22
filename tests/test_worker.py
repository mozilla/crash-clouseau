# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 python -m unittest tests.test_worker
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
# worker.py resolves a redis client at import; a valid-format URL is enough (lazy connect).
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import worker  # noqa: E402


class TestBlackHole(unittest.TestCase):
    def _job(self, jid="j1"):
        job = mock.MagicMock()
        job.args = ("uuid-1",)
        job.func_name = "crashclouseau.agent.orchestrator.run_evidence_agent"
        job.id = jid
        return job

    def test_logs_and_leaves_the_job_to_rq(self):
        job = self._job()
        with mock.patch.object(worker.logger, "error") as log:
            self.assertFalse(worker.black_hole(job))  # Stop the exception-handler chain.
        self.assertIn("run_evidence_agent('uuid-1',) failed", log.call_args.args[0])
        job.cancel.assert_not_called()


class TestFailureTtl(unittest.TestCase):
    def test_enqueued_jobs_keep_their_failure_for_thirty_days(self):
        queue = worker.get_queue("ttl-check")
        with mock.patch.object(worker.Queue, "enqueue_call") as base:
            queue.enqueue_call(func=print, result_ttl=0)
            self.assertEqual(base.call_args.kwargs["failure_ttl"], 30 * 24 * 3600)
            queue.enqueue_call(func=print, failure_ttl=60)
            self.assertEqual(base.call_args.kwargs["failure_ttl"], 60)


class TestWorkerRunsTheScheduler(unittest.TestCase):
    """`Retry(max=2, interval=[60, 300])` in `agent.orchestrator.enqueue_agent` is not
    self-executing: RQ parks the retry in the ScheduledJobRegistry and only a worker running
    the scheduler puts it back on the queue. `work()` defaults `with_scheduler` to False, so
    for two weeks the canary logged "requeuing" and requeued nothing — 54 stranded jobs, all
    overdue. This test is the whole guard against that returning."""

    def _worker(self):
        with mock.patch.object(worker, "Worker") as W:
            worker.main()
        return W

    def test_work_is_started_with_the_scheduler(self):
        self._worker().return_value.work.assert_called_once_with(with_scheduler=True)

    def test_the_exception_handler_is_still_wired(self):
        """Guards the extraction of `main()` out of the `__main__` block."""
        kwargs = self._worker().call_args.kwargs
        self.assertEqual(kwargs["exception_handlers"], [worker.black_hole])
        self.assertIs(kwargs["connection"], worker.conn)

    def test_it_consumes_the_queues_this_process_was_told_to(self):
        with mock.patch.object(worker, "listen", ["agent"]), \
             mock.patch.object(worker, "Queue") as Q, \
             mock.patch.object(worker, "Worker"):
            worker.main()
        self.assertEqual([c.args[0] for c in Q.call_args_list], ["agent"])


if __name__ == "__main__":
    unittest.main()
