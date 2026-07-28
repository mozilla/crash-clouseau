# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# bin/predeploy.py: which in-flight triage runs a deploy would actually destroy.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_predeploy
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import importlib.util  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "predeploy", os.path.join(_HERE, "..", "bin", "predeploy.py")
)
predeploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(predeploy)


def _row(uuid, status, minutes_old, cost=0.0, signature="Foo::bar"):
    return SimpleNamespace(
        uuid=uuid, status=status, signature=signature, cost_usd=cost,
        updated=datetime.now(timezone.utc) - timedelta(minutes=minutes_old),
        created=None,
    )


class TestInFlightRuns(unittest.TestCase):
    def setUp(self):
        # No dossier is reaper-owned unless a test says so (avoids touching the DB).
        p = mock.patch("crashclouseau.models.Dossier.get_stale_running", return_value=[])
        self.stale = p.start()
        self.addCleanup(p.stop)
        self.timeout_min = config.get_agent_job_timeout() / 60.0

    def test_live_running_blocks(self):
        runs = predeploy.in_flight_runs([_row("u-live", "running", 5)])
        self.assertEqual([r["uuid"] for r in runs], ["u-live"])
        self.assertAlmostEqual(runs[0]["age_s"] / 60.0, 5, places=1)

    def test_done_and_error_never_block(self):
        rows = [_row("u-done", "done", 1), _row("u-err", "error", 1)]
        self.assertEqual(predeploy.in_flight_runs(rows), [])

    def test_pending_blocks(self):
        # A retrigger reset that has not been picked up yet: killing the worker before
        # pickup is how a pending row gets stranded, so warn about it.
        runs = predeploy.in_flight_runs([_row("u-pend", "pending", 1)])
        self.assertEqual([r["uuid"] for r in runs], ["u-pend"])

    def test_run_past_job_timeout_does_not_block(self):
        # RQ has already SIGKILLed it, so a deploy costs it nothing extra.
        rows = [_row("u-dead", "running", self.timeout_min + 5)]
        self.assertEqual(predeploy.in_flight_runs(rows), [])

    def test_reaper_owned_does_not_block(self):
        self.stale.return_value = ["u-reaped"]
        rows = [_row("u-reaped", "running", 5), _row("u-live", "running", 5)]
        self.assertEqual([r["uuid"] for r in predeploy.in_flight_runs(rows)], ["u-live"])

    def test_unknown_age_still_blocks(self):
        # No `updated` -> assume it is alive rather than silently green-lighting a deploy.
        row = _row("u-noage", "running", 5)
        row.updated = None
        self.assertEqual([r["uuid"] for r in predeploy.in_flight_runs([row])], ["u-noage"])


class TestMeanRunCost(unittest.TestCase):
    def test_averages_completed_runs_only(self):
        rows = [
            _row("a", "done", 60, cost=2.0),
            _row("b", "done", 60, cost=4.0),
            _row("c", "running", 5, cost=0.0),    # in-flight cost is 0 until it finishes
            _row("d", "error", 60, cost=99.0),    # not a completed run
        ]
        self.assertAlmostEqual(predeploy.mean_run_cost(rows), 3.0)

    def test_no_completed_runs(self):
        self.assertEqual(predeploy.mean_run_cost([_row("a", "running", 5)]), 0.0)
        self.assertEqual(predeploy.mean_run_cost([]), 0.0)


class TestReport(unittest.TestCase):
    def test_clean_and_busy_wording(self):
        with mock.patch("builtins.print") as p:
            predeploy._report([], 3.0)
        self.assertIn("safe to deploy", " ".join(str(c) for c in p.call_args_list))
        runs = [{"uuid": "u-1", "status": "running", "age_s": 300, "signature": "S"}]
        with mock.patch("builtins.print") as p:
            predeploy._report(runs, 3.0)
        out = " ".join(str(c) for c in p.call_args_list)
        self.assertIn("STILL RUNNING", out)
        self.assertIn("$3.00", out)     # per-run estimate, and the total for 1 run
        # the status must not be printed twice (it read "running running 5m" once)
        self.assertEqual(out.count("running"), 1)


if __name__ == "__main__":
    unittest.main()
