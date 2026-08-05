# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Don't deploy on top of a triage run that is still working.

Heroku restarts every dyno on release: SIGTERM, then SIGKILL ~30 seconds later. An agent
run takes ~20 minutes, so it cannot possibly drain in that window -- the job is killed
mid-analysis, the orphan reaper re-enqueues it (up to ``reap_max_attempts``), and the whole
run starts over from nothing at roughly $3-4 a time. Measured on 2026-07-28: three deploys
inside an hour produced 11 re-enqueues.

Nothing here can stop Heroku killing a job, so the mitigation is simply to look first:

    uv run python bin/predeploy.py && git push heroku augmented:main
    uv run python bin/predeploy.py --wait        # ...or block until the queue drains

Exit 0 = nothing would be lost. Exit 1 = runs in flight (listed, with the spend that would
be thrown away). Add ``--force`` to report and exit 0 anyway.

Only runs that are still ALIVE block. Two kinds are excluded, because deploying costs them
nothing that is not already lost:

* a run past ``job_timeout`` -- RQ has already SIGKILLed its work-horse, and the dossier is
  just sitting at ``running`` until the reaper notices;
* anything the reaper already owns.

Queued-but-unstarted jobs don't block either: RQ keeps them in Redis, so they survive the
restart and run afterwards.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

# Whether the caller actually pointed us at a database. The fallbacks below exist so the
# module can be imported by tests, but a deploy guard reading an EMPTY sqlite would answer
# "nothing in flight" about a database that has no rows in it — so main() checks this and
# refuses rather than reporting.
_DB_CONFIGURED = bool(os.environ.get("DATABASE_URL"))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from crashclouseau import config, models  # noqa: E402
from crashclouseau.agent import orchestrator  # noqa: E402

_IN_FLIGHT = ("running", "pending")
_POLL_S = 30


def _age_s(updated):
    if updated is None:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds()


def mean_run_cost(rows):
    """Average cost of a COMPLETED run, for pricing what a deploy would throw away. An
    in-flight dossier's own ``cost_usd`` is 0 -- usage is recorded when the run finishes --
    so the loss has to be estimated from finished runs rather than read off the live rows."""
    costs = [r.cost_usd for r in rows if r.status == "done" and (r.cost_usd or 0) > 0]
    return sum(costs) / len(costs) if costs else 0.0


def in_flight_runs(rows):
    """``[{uuid, status, age_s, signature}]`` for runs a deploy would actually destroy."""
    # A run past job_timeout has already been SIGKILLed by RQ, so deploying costs it
    # nothing; the reaper's own threshold adds a grace buffer on top, which is the right
    # line for "who owns this" but too generous for "is this still alive".
    dead_after = config.get_agent_job_timeout()
    owned_by_reaper = set(models.Dossier.get_stale_running(
        dead_after + orchestrator._STALE_BUFFER_S))
    out = []
    for row in rows:
        if row.status not in _IN_FLIGHT or row.uuid in owned_by_reaper:
            continue
        age = _age_s(row.updated)
        if age is not None and age > dead_after:
            continue
        out.append({
            "uuid": row.uuid,
            "status": row.status,
            "age_s": age,
            "signature": row.signature or "",
        })
    return out


def _report(runs, per_run):
    if not runs:
        print("predeploy: nothing alive in flight — safe to deploy.")
        return
    print("predeploy: {} triage run(s) STILL RUNNING — a deploy kills these mid-analysis "
          "and they re-run from scratch:".format(len(runs)))
    for r in runs:
        age = "{:.0f}m".format(r["age_s"] / 60.0) if r["age_s"] is not None else "?"
        print("  {}  {:8s} {:>4s} in  {}".format(
            r["uuid"], r["status"], age, r["signature"][:48]))
    if per_run:
        print("predeploy: ~${:.2f} per completed run, so roughly ${:.2f} of work would be "
              "paid for twice.".format(per_run, per_run * len(runs)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wait", action="store_true",
                   help="poll until nothing is in flight, then exit 0")
    p.add_argument("--timeout", type=int, default=2400,
                   help="give up waiting after N seconds (default 2400 = 40min)")
    p.add_argument("--force", action="store_true",
                   help="report but always exit 0")
    args = p.parse_args()

    if not _DB_CONFIGURED:
        # Previously this fell through to the in-memory sqlite default and died on
        # "no such table: dossiers" under 40 lines of SQLAlchemy traceback. It exited 1, so
        # `&& git push` was still safe, but nothing in the output said what was wrong.
        print("predeploy: DATABASE_URL is not set, so there is no way to tell what is "
              "running.\n"
              "  DATABASE_URL=\"$(heroku config:get DATABASE_URL -a crash-clouseau-augmented)\" "
              "\\\n    uv run python bin/predeploy.py", file=sys.stderr)
        return 1

    deadline = time.time() + args.timeout
    while True:
        try:
            rows = models.Dossier.list_tasks()
        except Exception as exc:
            # Refuse, never report. "Cannot see the queue" must not read like "queue empty":
            # this function's exit code gates a deploy that kills ~$3-4 runs mid-analysis.
            print("predeploy: cannot read the dossiers table ({}: {}) — refusing rather than "
                  "guessing that nothing is in flight.".format(type(exc).__name__,
                                                               str(exc).splitlines()[0][:120]),
                  file=sys.stderr)
            return 1
        runs = in_flight_runs(rows)
        _report(runs, mean_run_cost(rows))
        if not runs:
            return 0
        if not args.wait:
            break
        if time.time() >= deadline:
            print("predeploy: still busy after {}s — giving up on waiting.".format(
                args.timeout))
            break
        print("predeploy: waiting {}s…".format(_POLL_S), flush=True)
        time.sleep(_POLL_S)
    return 0 if args.force else 1


if __name__ == "__main__":
    sys.exit(main())
