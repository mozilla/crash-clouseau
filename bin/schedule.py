# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from apscheduler.schedulers.blocking import BlockingScheduler
from crashclouseau import feedback, update
from crashclouseau.agent import orchestrator


sched = BlockingScheduler(timezone="GMT")


@sched.scheduled_job("interval", minutes=20)
def timed_job():
    update.update_all()


@sched.scheduled_job("interval", minutes=15)
def reap_orphans_job():
    # Re-enqueue triage runs orphaned by a dyno restart (dossier stuck "running"), so
    # they self-heal instead of blocking that crash forever.
    orchestrator.reap_stale_agent_jobs()


@sched.scheduled_job("interval", hours=6)
def sweep_untriaged_job():
    # Offer the agent the crashes it was never given at all — a job lost with the Redis queue
    # leaves no dossier for the reaper to find. Deliberately six-hourly rather than 15-minutely:
    # this one SPENDS (~$3 a crash), it is bounded per tick, and nothing about it is urgent.
    orchestrator.sweep_untriaged_crashes()


@sched.scheduled_job("interval", hours=6)
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


sched.start()
