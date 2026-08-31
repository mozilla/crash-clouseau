# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Agent tasks (monitoring) view: the pure _task_view aggregator + the /tasks.html
# route. Runs with no real DB / no real network:
#   DATABASE_URL=sqlite:// python -m unittest tests.test_tasks_view
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import re  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import app, html, models  # noqa: E402


NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
STALE = 2100  # job_timeout (1800) + buffer (300)


def _row(**kw):
    base = dict(
        uuid="0" * 36,
        signature="sig",
        status="done",
        created=NOW - timedelta(minutes=20),
        updated=NOW - timedelta(minutes=2),
        cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        worker_models=None,
        verdict=None,
        confidence=None,
        filed_bug=None,
        filed_mode=None,
        filed_needinfo=None,
        run_started=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestTheChannelReachesTheView(unittest.TestCase):
    """`Dossier.list_tasks` selects `Build.channel`/`Build.version` and `templates/tasks.html`
    renders them, but `_task_view` is a literal dict between the two and did not name either —
    so every row rendered `?` / `unknown`, on both channels, from the day the column landed.

    Jinja resolves a missing key to `Undefined`, which is falsy, so `{{ (t.channel or '?') }}`
    swallowed it silently: the column existed, was uniformly empty, and read as "channel
    unknown" rather than as a missing feature. It is the day-one observable both `DEPLOY.md`
    and `next_session.md` point an operator at for the beta rollout, and at ~4-6 beta dossiers
    a day against nightly's 85-120 an unlabelled beta row cannot be found by eye.

    `_row` here does not set `channel`, deliberately: the view must keep tolerating a row
    without one (`getattr` default), which is also why adding the key could not have been
    caught by the existing tests."""

    def test_the_channel_and_version_reach_the_task_dict(self):
        tasks, _ = html._task_view([_row(channel="beta", version="155.0b4")], STALE, NOW)
        self.assertEqual(tasks[0]["channel"], "beta")
        self.assertEqual(tasks[0]["version"], "155.0b4")

    def test_a_row_with_no_build_still_renders(self):
        """`uuids.buildid` is a nullable FK and `list_tasks` joins `builds` OUTER on purpose —
        an invisible stalled run is the exact failure the view exists to catch."""
        tasks, _ = html._task_view([_row()], STALE, NOW)
        self.assertIsNone(tasks[0]["channel"])
        self.assertIsNone(tasks[0]["version"])

    def test_the_template_prints_the_initial_not_a_question_mark(self):
        from flask import render_template

        from crashclouseau import app

        tasks, summary = html._task_view(
            [_row(uuid="b" * 36, channel="beta", version="155.0b4"),
             _row(uuid="n" * 36, channel="nightly", version="156.0a1")], STALE, NOW)
        with app.test_request_context():
            out = render_template("tasks.html", tasks=tasks, summary=summary,
                                  stale_after=STALE, now=NOW)
        self.assertIn('title="beta 155.0b4">B<', out)
        self.assertIn('title="nightly 156.0a1">N<', out)
        self.assertNotIn('title="unknown">?<', out)


class TestTaskView(unittest.TestCase):
    def test_empty(self):
        tasks, summary = html._task_view([], STALE, NOW)
        self.assertEqual(tasks, [])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["pct_done"], 0)
        self.assertEqual(summary["cost_total"], 0.0)
        self.assertEqual(summary["cost_avg"], 0.0)
        self.assertIsNone(summary["duration_avg_s"])
        self.assertEqual(summary["duration_avg_str"], "—")

    def test_done_duration_is_run_time_and_costs_aggregate(self):
        rows = [_row(status="done", cost_usd=0.42)]
        tasks, summary = html._task_view(rows, STALE, NOW)
        # done duration = updated - created = 18 min = 1080s (NOT elapsed-since-now)
        self.assertAlmostEqual(tasks[0]["duration_s"], 18 * 60)
        self.assertEqual(tasks[0]["duration_str"], "18m")
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["pct_done"], 100)
        self.assertAlmostEqual(summary["cost_total"], 0.42)
        self.assertAlmostEqual(summary["cost_avg"], 0.42)
        self.assertAlmostEqual(summary["duration_avg_s"], 1080)

    def test_running_fresh_is_not_stalled_and_duration_is_elapsed(self):
        row = _row(
            status="running",
            created=NOW - timedelta(minutes=5),
            updated=NOW - timedelta(minutes=5),
        )
        tasks, summary = html._task_view([row], STALE, NOW)
        self.assertFalse(tasks[0]["stalled"])
        self.assertEqual(summary["stalled"], 0)
        # running duration = now - created (elapsed so far), not updated - created
        self.assertAlmostEqual(tasks[0]["duration_s"], 5 * 60)

    def test_duration_times_the_attempt_not_the_row(self):
        """A retriggered crash keeps its original `created` (reset_for_retrigger leaves it
        alone deliberately), so timing from it reported runs that had started 20 minutes
        earlier as "29h running" -- during a bulk recovery that reads as a hung fleet.
        `run_started` is stamped when the attempt claims the row."""
        row = _row(
            status="running",
            created=NOW - timedelta(hours=29),          # first ingested yesterday
            updated=NOW - timedelta(minutes=1),         # beating
            run_started=(NOW - timedelta(minutes=20)).isoformat(),
        )
        tasks, _ = html._task_view([row], STALE, NOW)
        self.assertAlmostEqual(tasks[0]["duration_s"], 20 * 60)
        self.assertEqual(tasks[0]["duration_str"], "20m")

    def test_done_duration_also_measures_the_attempt(self):
        row = _row(
            status="done",
            created=NOW - timedelta(hours=29),
            updated=NOW,
            run_started=(NOW - timedelta(minutes=16)).isoformat(),
        )
        tasks, _ = html._task_view([row], STALE, NOW)
        self.assertAlmostEqual(tasks[0]["duration_s"], 16 * 60)

    def test_duration_falls_back_to_created_without_a_run_started(self):
        # Rows written before `run_started` existed, and the tests' hand-built rows.
        row = _row(status="running", created=NOW - timedelta(minutes=7),
                   updated=NOW - timedelta(minutes=1))
        tasks, _ = html._task_view([row], STALE, NOW)
        self.assertAlmostEqual(tasks[0]["duration_s"], 7 * 60)

    def test_an_unparseable_run_started_does_not_break_the_page(self):
        row = _row(status="running", created=NOW - timedelta(minutes=7),
                   updated=NOW - timedelta(minutes=1), run_started="not-a-timestamp")
        tasks, _ = html._task_view([row], STALE, NOW)
        self.assertAlmostEqual(tasks[0]["duration_s"], 7 * 60)

    def test_cost_avg_is_per_done_run_not_per_costed_row(self):
        """The template labels it avg/done. An errored or abandoned run keeps the cost it
        burned before dying, so dividing by every costed row understates the real
        per-result cost -- and understates it MORE the worse the failure rate, i.e. it
        looks best exactly when things are worst."""
        rows = [
            _row(status="done", cost_usd=3.00),
            _row(status="done", cost_usd=3.00),
            _row(status="error", cost_usd=0.20),   # died early, still cost something
            _row(status="running", cost_usd=0.10),
        ]
        _, s = html._task_view(rows, STALE, NOW)
        self.assertAlmostEqual(s["cost_total"], 6.30)   # total is still everything spent
        self.assertAlmostEqual(s["cost_avg"], 3.00)     # not 6.30/4 = 1.575

    def test_cost_avg_is_zero_when_nothing_finished(self):
        _, s = html._task_view([_row(status="error", cost_usd=0.20)], STALE, NOW)
        self.assertEqual(s["cost_avg"], 0.0)

    def test_cost_avg_skips_a_done_run_with_no_recorded_cost(self):
        # The mirror of the bug being fixed: counting a cost-less finished run as $0 drags
        # the average down just as including part-runs did.
        rows = [_row(status="done", cost_usd=3.00), _row(status="done", cost_usd=None)]
        _, s = html._task_view(rows, STALE, NOW)
        self.assertAlmostEqual(s["cost_avg"], 3.00)   # not 1.50
        self.assertEqual(s["done"], 2)

    def test_running_past_threshold_is_stalled(self):
        row = _row(
            status="running",
            created=NOW - timedelta(hours=2),
            updated=NOW - timedelta(minutes=90),  # 5400s > STALE
        )
        tasks, summary = html._task_view([row], STALE, NOW)
        self.assertTrue(tasks[0]["stalled"])
        self.assertEqual(summary["stalled"], 1)
        self.assertEqual(summary["running"], 1)

    def test_naive_timestamps_treated_as_utc(self):
        # sqlite returns naive datetimes; arithmetic must still work.
        row = _row(
            status="done",
            created=(NOW - timedelta(minutes=10)).replace(tzinfo=None),
            updated=NOW.replace(tzinfo=None),
        )
        tasks, _ = html._task_view([row], STALE, NOW)
        self.assertAlmostEqual(tasks[0]["duration_s"], 10 * 60)

    def test_mixed_fleet_summary(self):
        rows = [
            _row(status="done", cost_usd=0.40),
            _row(status="running", created=NOW - timedelta(minutes=5),
                 updated=NOW - timedelta(minutes=5)),
            _row(status="running", created=NOW - timedelta(hours=3),
                 updated=NOW - timedelta(hours=2)),  # stalled
            _row(status="error", created=NOW - timedelta(minutes=10),
                 updated=NOW - timedelta(minutes=9)),
            _row(status="pending", created=NOW - timedelta(minutes=1),
                 updated=NOW - timedelta(minutes=1)),
        ]
        _, s = html._task_view(rows, STALE, NOW)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["done"], 1)
        self.assertEqual(s["running"], 2)
        self.assertEqual(s["error"], 1)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["stalled"], 1)
        self.assertEqual(s["pct_done"], 20)
        # only the done task carried a cost -> avg is over costed tasks, not all
        self.assertAlmostEqual(s["cost_total"], 0.40)
        self.assertAlmostEqual(s["cost_avg"], 0.40)


class TestFiledBugColumn(unittest.TestCase):
    """The Bug column surfaces what the autofiler did. Rows predating automatic filing (and
    every run it declined to file) carry no such fields at all, which is why `_task_view`
    reads them with `getattr` defaults rather than attribute access."""

    def setUp(self):
        self.client = app.test_client()

    def _render(self, rows):
        with mock.patch.object(html.models.Dossier, "list_tasks", return_value=rows):
            rv = self.client.get("/tasks.html")
        self.assertEqual(rv.status_code, 200)
        return rv.get_data(as_text=True)

    def test_a_filed_bug_links_to_bugzilla(self):
        body = self._render([_row(uuid="filed001" + "0" * 28, verdict="lead",
                                  confidence=0.7, filed_bug="1979234",
                                  filed_mode="new_bug",
                                  filed_needinfo="dev@moz.example")])
        self.assertIn("https://bugzilla.mozilla.org/show_bug.cgi?id=1979234", body)
        self.assertIn("bug&nbsp;1979234", body)
        self.assertIn("dev@moz.example", body)   # the needinfo target, in the tooltip
        self.assertIn("ni?", body)
        self.assertNotIn(">cmt<", body)

    def test_a_needinfo_we_could_not_set_is_shown_as_a_gap(self):
        """An hg commit address is often not a Bugzilla login, and BMO refuses a create whose
        requestee it cannot resolve — so the bug files without one. That run must not render
        identically to one where nobody needed asking: the whole purpose of the filing is to
        put the crash in front of a person, and here nobody has been."""
        body = self._render([_row(uuid="noni0001" + "0" * 28, verdict="lead",
                                  confidence=0.7, filed_bug="1979235",
                                  filed_mode="new_bug", filed_needinfo=None,
                                  filed_needinfo_missed="farre@mozilla.com")])
        self.assertIn("no&nbsp;ni", body)
        self.assertIn("farre@mozilla.com", body)      # who we wanted, in the tooltip
        self.assertNotIn(">ni?<", body)               # never claim someone was asked

    def test_rows_without_the_new_field_still_render(self):
        # `list_tasks` gained `filed_needinfo_missed` after these rows existed; `_task_view`
        # reads it with a getattr default for exactly this reason.
        body = self._render([_row(uuid="old00001" + "0" * 28, filed_bug="1979236",
                                  filed_mode="new_bug", filed_needinfo="dev@moz.example")])
        self.assertIn("ni?", body)
        self.assertNotIn("no&nbsp;ni", body)

    def test_a_comment_on_an_existing_bug_is_marked_as_such(self):
        # Worth distinguishing: that bug is somebody else's and we added to it.
        body = self._render([_row(uuid="cmt00001" + "0" * 28,
                                  filed_bug="1863047",
                                  filed_mode="comment_on_existing")])
        self.assertIn("show_bug.cgi?id=1863047", body)
        self.assertIn(">cmt<", body)
        self.assertIn("instead of filing a duplicate", body)

    def test_rows_without_filing_render_a_dash_not_an_error(self):
        # The normal case for every run before filing was armed.
        body = self._render([_row(uuid="nofile01" + "0" * 28)])
        self.assertNotIn("show_bug.cgi", body)
        self.assertIn("bugs filed", body)        # the summary tile still renders

    def test_the_summary_counts_only_filed_rows(self):
        rows = [
            _row(uuid="a" * 36, filed_bug="111"),
            _row(uuid="b" * 36, filed_bug="222", filed_mode="comment_on_existing"),
            _row(uuid="c" * 36),                                   # not filed
            _row(uuid="d" * 36, filed_bug=None),                   # explicitly null
        ]
        _, summary = html._task_view(rows, STALE, NOW)
        self.assertEqual(summary["filed"], 2)

    def test_legacy_rows_without_the_fields_do_not_break(self):
        row = _row()
        for f in ("filed_bug", "filed_mode", "filed_needinfo"):
            delattr(row, f)
        tasks, summary = html._task_view([row], STALE, NOW)
        self.assertIsNone(tasks[0]["filed_bug"])
        self.assertEqual(summary["filed"], 0)


class TestTasksRoute(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_renders_rows(self):
        rows = [
            _row(uuid="abcdef01" + "0" * 28, signature="mozilla::Foo",
                 status="done", cost_usd=0.37, input_tokens=1000,
                 output_tokens=200, cache_read_tokens=5000,
                 verdict="lead", confidence=0.8),
            _row(uuid="stalled1" + "0" * 28, status="running",
                 created=NOW - timedelta(hours=3),
                 updated=NOW - timedelta(hours=2)),
        ]
        with mock.patch.object(html.models.Dossier, "list_tasks", return_value=rows):
            rv = self.client.get("/tasks.html")
        self.assertEqual(rv.status_code, 200)
        body = rv.get_data(as_text=True)
        # short-uuid link into the detailed crash view
        self.assertIn("/crashstack.html?uuid=abcdef01", body)
        self.assertIn(">abcdef01<", body)
        # status + verdict + cost surfaced
        self.assertIn("status-done", body)
        self.assertIn("lead", body)
        self.assertIn("$0.37", body)
        # the second row is a stalled orphan
        self.assertIn("stalled", body)
        # a running task gets a retrigger button; the done task does not (1 button total)
        self.assertIn("retriggerTask('stalled1", body)
        self.assertEqual(body.count("retriggerTask("), 1)

    def test_error_task_gets_retrigger_button(self):
        rows = [_row(uuid="err00001" + "0" * 28, status="error")]
        with mock.patch.object(html.models.Dossier, "list_tasks", return_value=rows):
            rv = self.client.get("/tasks.html")
        self.assertIn("retriggerTask('err00001", rv.get_data(as_text=True))

    def test_zero_tokens_render_as_dash(self):
        # A row with no token data (old run / not finished) shows a dash, not 0/0/0.
        rows = [
            _row(uuid="withtok0" + "0" * 28, status="done",
                 input_tokens=5000, output_tokens=100, cache_read_tokens=200),
            _row(uuid="notok000" + "0" * 28, status="done",
                 input_tokens=0, output_tokens=0, cache_read_tokens=0),
        ]
        with mock.patch.object(html.models.Dossier, "list_tasks", return_value=rows):
            body = self.client.get("/tasks.html").get_data(as_text=True)
        self.assertIn("5000", body)                       # real tokens shown
        self.assertNotIn("0&nbsp;/&nbsp;0&nbsp;/&nbsp;0", body)  # zero row -> dash

    def test_empty_shows_placeholder(self):
        with mock.patch.object(html.models.Dossier, "list_tasks", return_value=[]):
            rv = self.client.get("/tasks.html")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("No triage runs yet", rv.get_data(as_text=True))


class TestTaskColumnWidths(unittest.TestCase):
    """The tasks table is `table-layout: fixed; width: 100%`, so its column widths are not
    cosmetic -- they are the layout. A column the CSS does not name gets only what the named
    ones leave over, and adding the Bug column left the 9-rule list summing to 100% with a
    10th column to place, which put the Actions cell outside the table and shifted every
    width from 5 onwards onto the wrong column.

    Nothing else catches this: the route still returns 200, every rendering assertion still
    passes, and the damage is visible only to somebody looking at the page. So assert the
    two invariants directly against the shipped files."""

    _CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "static", "clouseau.css")
    _TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "tasks.html")

    def _header_cells(self):
        with open(self._TPL, encoding="utf-8") as fh:
            tpl = fh.read()
        # Scope to the tasks table's own <thead>: other tables must not affect the count.
        head = re.search(r'<table class="tasks">.*?<thead>(.*?)</thead>', tpl, re.S)
        self.assertIsNotNone(head, "tasks table <thead> not found")
        return re.findall(r"<th\b", head.group(1))

    def _widths(self):
        with open(self._CSS, encoding="utf-8") as fh:
            css = fh.read()
        return {
            int(n): float(w)
            for n, w in re.findall(
                r"\.tasks th:nth-child\((\d+)\)[^{]*\{\s*width:\s*([\d.]+)%", css)
        }

    def test_every_column_has_a_width(self):
        widths = self._widths()
        self.assertEqual(sorted(widths), list(range(1, len(self._header_cells()) + 1)),
                         "each <th> in tasks.html needs a .tasks th:nth-child(N) width rule "
                         "-- an unnamed column renders outside the table")

    def test_widths_fill_the_table_exactly(self):
        self.assertAlmostEqual(sum(self._widths().values()), 100.0, places=6)

    def test_empty_row_colspan_matches(self):
        """`colspan` on the "no runs yet" row is the same drift with a smaller blast radius:
        it under-spans the table the moment a column is added."""
        with open(self._TPL, encoding="utf-8") as fh:
            tpl = fh.read()
        self.assertIn('colspan="{}"'.format(len(self._header_cells())), tpl)


class TestRetriggerEndpoint(unittest.TestCase):
    """These two tests USED TO PASS ANONYMOUSLY, which is how the route stayed unauthenticated
    for its whole life: they asserted the happy path and the missing-uuid path, and a route that
    spends $1.70 a call satisfied both without a token. The authorization arms live in
    `tests/test_retrigger_auth.py`; what stays here is the plumbing."""

    _TOKEN = "tasks-view-token"

    def setUp(self):
        self.client = app.test_client()
        self.env = mock.patch.dict(
            os.environ, {"API_WRITE_TOKEN": self._TOKEN}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _headers(self):
        return {"X-Clouseau-Token": self._TOKEN}

    def test_retrigger_posts_to_orchestrator(self):
        from crashclouseau.agent import orchestrator
        with mock.patch.object(orchestrator, "retrigger_agent",
                               return_value={"uuid": "u-1", "cancelled": True}) as rt, \
                mock.patch.object(models.UUID, "exists", return_value=True):
            rv = self.client.post("/api/tasks/retrigger", json={"uuid": "u-1"},
                                  headers=self._headers())
        self.assertEqual(rv.status_code, 200)
        rt.assert_called_once_with("u-1")
        self.assertEqual(rv.get_json(), {"uuid": "u-1", "cancelled": True})

    def test_retrigger_requires_uuid(self):
        rv = self.client.post("/api/tasks/retrigger", json={}, headers=self._headers())
        self.assertEqual(rv.status_code, 400)


if __name__ == "__main__":
    unittest.main()
