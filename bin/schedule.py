# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import functools

from apscheduler.schedulers.blocking import BlockingScheduler
from crashclouseau import app, feedback, update
from crashclouseau.agent import orchestrator


sched = BlockingScheduler(timezone="GMT")


def scheduled(**interval):
    """Register a clock job AND give its body a Flask app context.

    EVERY job needs one and none of them gets one for free. ``crashclouseau/__init__.py``
    pushes an app context at import time, but that is the MAIN thread; APScheduler runs each
    job in a ``ThreadPoolExecutor`` worker, and a new thread starts from a fresh
    ``contextvars`` context — so ``db.session`` has no registry key and the first query raises
    ``RuntimeError: Working outside of application context``.

    It had already been fixed twice, PER FUNCTION: ``reap_stale_agent_jobs`` and
    ``sweep_untriaged_crashes`` each push their own, with a comment saying why. That is
    knowledge a new job does not inherit, and ``feedback.refresh`` was the third one.
    It had only ever been called from ``bin/feedback.py`` — the main thread, where the
    import-time push applies — so it needed no context there and got none here. On the clock
    it raised on every 6-hourly tick from 2026-08-21 (``a5e86e7``) to 2026-08-26 and wrote
    nothing at all: the ``feedback`` table stayed at 43 rows, every one stamped with a single
    manual run on 2026-08-17, while 27 filed bugs accumulated no recorded outcome.

    It hid for five days because it fails by DOING NOTHING and because of the arithmetic of
    the intervals: 6h is a common multiple of 20m and 15m, so at six hours after boot all four
    jobs fire in the same second and the traceback is logged between the other three jobs'
    "executed successfully" lines.

    Doing it HERE rather than in each job is the whole point — the context is a property of
    being run by this scheduler, not of any one job, so a job added later cannot forget it.
    The two inner pushes stay: nested contexts are harmless, and they still protect those
    functions for callers that are not the clock (the heartbeat thread is a third such
    caller). ``tests/test_clock_jobs.py`` enumerates ``sched.get_jobs()``, so this cannot
    silently stop covering a new one."""
    def decorate(func):
        @functools.wraps(func)   # keeps APScheduler's job name, so the log lines are unchanged
        def run_in_context():
            with app.app_context():
                return func()

        return sched.scheduled_job("interval", **interval)(run_in_context)

    return decorate


@scheduled(minutes=20)
def timed_job():
    update.update_all()


@scheduled(minutes=15)
def reap_orphans_job():
    # Re-enqueue triage runs orphaned by a dyno restart (dossier stuck "running"), so
    # they self-heal instead of blocking that crash forever.
    orchestrator.reap_stale_agent_jobs()


@scheduled(hours=6)
def sweep_untriaged_job():
    # Offer the agent the crashes it was never given at all — a job lost with the Redis queue
    # leaves no dossier for the reaper to find. Deliberately six-hourly rather than 15-minutely:
    # this one SPENDS (~$3 a crash), it is bounded per tick, and nothing about it is urgent.
    orchestrator.sweep_untriaged_crashes()


@scheduled(hours=6)
def feedback_job():
    # Read back what became of the bugs we filed — the structured outcome (resolution,
    # regressed_by) and the reviewers' COMMENTS, which is where every correction has actually
    # been written. `refresh()` has always been safe to run on a schedule and never was on
    # one: it was reachable only from `bin/feedback.py`, so the table only ever held whatever
    # the last manual run saw, and the repo could not read a rebuttal at all.
    #
    # Six-hourly, matching `sweep_untriaged_job`, not hourly: BMO rate-limits an IP for ~45
    # minutes when pushed, and the traffic being watched arrives at ~2.2 bugs per six hours
    # (measured over the 52 filings' last 7 days; busiest bucket 9). Read-only and
    # unauthenticated, so unlike the sweep above it spends nothing.
    feedback.refresh()


# Importable without starting the clock, so `tests/test_clock_jobs.py` can enumerate the
# registered jobs and run each one in a worker thread. `Procfile` runs this as a script.
if __name__ == "__main__":
    sched.start()
