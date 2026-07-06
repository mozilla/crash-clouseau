# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from apscheduler.schedulers.blocking import BlockingScheduler
from crashclouseau import update
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


sched.start()
