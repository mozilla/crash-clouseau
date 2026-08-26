# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Every job on the clock must be able to touch the database.

THE DEFECT THIS PINS. `crashclouseau/__init__.py` pushes a Flask app context at import time,
in the MAIN thread. APScheduler runs each scheduled job in a `ThreadPoolExecutor` worker, and
a new thread starts from a fresh `contextvars` context, so `db.session` has no registry key
and the first query raises `RuntimeError: Working outside of application context`.

`feedback.refresh` went onto the clock in `a5e86e7` (2026-08-21) without a context, because it
had only ever been called from `bin/feedback.py` — the main thread, where the import-time push
applies. It then raised on every 6-hourly tick for five days and wrote NOTHING: the `feedback`
table stayed at 43 rows all stamped 2026-08-17 (one manual run) while 27 filed bugs
accumulated no recorded outcome. Nothing failed loudly; the traceback was logged between the
other three jobs' "executed successfully" lines, because 6h is a common multiple of 20m and
15m so at six hours after boot all four jobs fire in the same second.

WHY IT ENUMERATES `sched.get_jobs()` RATHER THAN NAMING THE FOUR. The same bug had already
been fixed twice per-function (`reap_stale_agent_jobs`, `sweep_untriaged_crashes`), and
per-function knowledge is exactly what the third job did not inherit. A test that lists the
jobs it knows about would not have caught `feedback_job` either. This one fails for any job
added later that the decorator does not cover.
"""
# The package builds a Flask app at import, so a URL must exist before the import below.
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import concurrent.futures as cf  # noqa: E402
import importlib.util  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

import sqlalchemy as sa  # noqa: E402

from crashclouseau import db, feedback, update  # noqa: E402
from crashclouseau.agent import orchestrator  # noqa: E402


def _load_schedule():
    """Import `bin/schedule.py` without starting the blocking scheduler.

    `bin/` is not a package, and the module guards `sched.start()` behind `__main__` for
    exactly this reason — the `Procfile` runs it as a script, so production is unaffected."""
    path = Path(__file__).resolve().parent.parent / "bin" / "schedule.py"
    spec = importlib.util.spec_from_file_location("clock_schedule_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCHEDULE = _load_schedule()

# What each job calls. Patched with a probe so the test exercises the WIRING (does the
# registered callable give its body an app context) and never the job's real work, which
# would hit Socorro, hg, BMO, Redis and the agent budget.
_TARGETS = (
    (update, "update_all"),
    (orchestrator, "reap_stale_agent_jobs"),
    (orchestrator, "sweep_untriaged_crashes"),
    (feedback, "refresh"),
)


def _probe():
    """The smallest thing every one of these jobs does: use the scoped session."""
    db.session.execute(sa.text("select 1"))


class TestEveryClockJobRunsInAnAppContext(unittest.TestCase):

    def setUp(self):
        self.calls = []

        def probe():
            self.calls.append(True)
            _probe()

        for module, name in _TARGETS:
            patcher = mock.patch.object(module, name, probe)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_probe_fails_in_a_worker_thread_without_a_context(self):
        """FIRST, because it is what makes the next test mean anything. If a bare
        `db.session` worked from a worker thread, the suite below would pass with or without
        the decorator and would have been green all through the five days it was broken."""
        with cf.ThreadPoolExecutor(1) as pool:
            with self.assertRaises(RuntimeError) as caught:
                pool.submit(_probe).result()
        self.assertIn("Working outside of application context", str(caught.exception))

    def test_every_scheduled_job_gets_an_app_context(self):
        """Run each REGISTERED callable the way APScheduler does — in a worker thread — and
        require that its body can reach the database."""
        jobs = _SCHEDULE.sched.get_jobs()
        self.assertTrue(jobs, "no jobs registered; the clock would do nothing")
        for job in jobs:
            with self.subTest(job=job.name):
                self.calls.clear()
                with cf.ThreadPoolExecutor(1) as pool:
                    pool.submit(job.func).result()   # must not raise
                self.assertEqual(len(self.calls), 1, "job body did not run")

    def test_the_feedback_job_is_still_on_the_clock(self):
        """The job that was silently dead. Its absence would look exactly like its failure —
        no rows, no error — so the schedule itself is asserted rather than inferred."""
        names = {job.name for job in _SCHEDULE.sched.get_jobs()}
        self.assertIn("feedback_job", names)
        self.assertEqual(
            names, {"timed_job", "reap_orphans_job", "sweep_untriaged_job", "feedback_job"})

    def test_the_job_names_survive_the_decorator(self):
        """`functools.wraps`, so the operator-facing log line is unchanged. Every diagnosis of
        this class of bug starts from `Job "<name>" ... executed successfully` versus `raised
        an exception`, and a wrapper that renamed them all to `run_in_context` would make the
        next one unreadable."""
        for job in _SCHEDULE.sched.get_jobs():
            self.assertNotEqual(job.name, "run_in_context")


if __name__ == "__main__":
    unittest.main()
