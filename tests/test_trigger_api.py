# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""``POST /api/tasks/trigger``: analyse one crash (or a short list) on request, choosing whether
the run may file a bug and whether it shows on tasks.html.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_trigger_api

No network, no Postgres: Socorro, the models and the orchestrator are stood in for.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from sqlalchemy.dialects import postgresql  # noqa: E402

from crashclouseau import app, bugzilla_apply, config, models, tools, trigger, update, utils  # noqa: E402
from crashclouseau.agent import orchestrator  # noqa: E402

_UUID = "6f86db23-6613-44d1-ac89-4963b0260907"
_UUID2 = "7fcc1b79-5113-4ef1-9f8d-ac7bd0260906"
_TOKEN = "write-token"
_HEADERS = {"X-Clouseau-Token": _TOKEN}


def _processed(**kw):
    base = {"uuid": _UUID, "product": "Firefox", "release_channel": "release",
            "version": "155.0.1", "build": "20260903215306",
            "signature": "SplitSingleCharHelper",
            "proto_signature": "SplitSingleCharHelper | js::StringSplitString"}
    base.update(kw)
    return base


class _Retrigger:
    def __init__(self):
        self.calls = []

    def __call__(self, uuid, channel=None):
        self.calls.append((uuid, channel))
        return {"uuid": uuid, "cancelled": False, "already_filed": None}


class _WithToken(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        env = mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.retrigger = _Retrigger()
        p = mock.patch.object(orchestrator, "retrigger_agent", self.retrigger)
        p.start()
        self.addCleanup(p.stop)

    def post(self, body, headers=_HEADERS):
        return self.client.post("/api/tasks/trigger", json=body, headers=headers)


class TestAuth(_WithToken):
    """It spends money per uuid and can reach Bugzilla: the WRITE token, in a header, only."""

    def test_anonymous_is_refused(self):
        rv = self.post({"uuid": _UUID}, headers={})
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(self.retrigger.calls, [])

    def test_a_wrong_token_is_refused(self):
        rv = self.post({"uuid": _UUID}, headers={"X-Clouseau-Token": "nope"})
        self.assertEqual(rv.status_code, 403)

    def test_the_query_string_token_does_not_open_it(self):
        rv = self.client.post("/api/tasks/trigger?token=" + _TOKEN, json={"uuid": _UUID})
        self.assertEqual(rv.status_code, 403)

    def test_no_token_configured_means_closed(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": ""}, clear=False):
            rv = self.post({"uuid": _UUID})
        self.assertEqual(rv.status_code, 503)


class TestValidation(_WithToken):
    def test_a_body_is_required(self):
        self.assertEqual(self.post({}).status_code, 400)
        self.assertEqual(self.post({"uuids": []}).status_code, 400)
        self.assertEqual(self.post({"uuids": "not-a-list"}).status_code, 400)
        self.assertEqual(self.post({"uuids": [1, 2]}).status_code, 400)

    def test_at_most_twenty(self):
        rv = self.post({"uuids": ["%032x" % i for i in range(21)]})
        self.assertEqual(rv.status_code, 400)

    def test_the_flags_must_be_booleans(self):
        self.assertEqual(self.post({"uuid": _UUID, "file_bug": "yes"}).status_code, 400)
        self.assertEqual(self.post({"uuid": _UUID, "show_in_tasks": 1}).status_code, 400)
        self.assertEqual(self.retrigger.calls, [])

    def test_a_malformed_uuid_is_a_per_item_error(self):
        rv = self.post({"uuids": ["not-a-uuid"]})
        self.assertEqual(rv.status_code, 200)
        res = rv.get_json()["results"]
        self.assertEqual((res[0]["ok"], res[0]["error"]), (False, "not a crash uuid"))
        self.assertEqual(self.retrigger.calls, [])


class TestAnIngestedCrash(_WithToken):
    """The uuid is already ours: record the options, then a forced re-run."""

    def setUp(self):
        super().setUp()
        self.options = []
        for p in (mock.patch.object(models.UUID, "exists", return_value=True),
                  mock.patch.object(models.UUID, "get_channel", return_value="nightly"),
                  mock.patch.object(models.UUID, "get_signature", return_value="sig"),
                  mock.patch.object(models.Dossier, "set_run_options",
                                    side_effect=lambda u, o, commit=True: self.options.append((u, o)) or True)):
            p.start()
            self.addCleanup(p.stop)

    def test_defaults_are_no_filing_and_listed(self):
        rv = self.post({"uuid": _UUID})
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertEqual((body["file_bug"], body["show_in_tasks"]), (False, True))
        res = body["results"][0]
        self.assertEqual((res["ok"], res["action"], res["ingested"], res["channel"]),
                         (True, "queued", False, "nightly"))
        uuid, opts = self.options[0]
        self.assertEqual((uuid, opts["autofile"], opts["show_in_tasks"], opts["source"]),
                         (_UUID, False, True, "api"))
        self.assertEqual(self.retrigger.calls, [(_UUID, "nightly")])

    def test_the_flags_reach_the_options(self):
        self.post({"uuid": _UUID, "file_bug": True, "show_in_tasks": False})
        _, opts = self.options[0]
        self.assertEqual((opts["autofile"], opts["show_in_tasks"]), (True, False))

    def test_a_list_runs_each_once_in_order(self):
        rv = self.post({"uuids": [_UUID, _UUID2, _UUID]})
        res = rv.get_json()["results"]
        self.assertEqual([r["uuid"] for r in res], [_UUID, _UUID2])
        self.assertEqual([c[0] for c in self.retrigger.calls], [_UUID, _UUID2])


class TestIngest(unittest.TestCase):
    """A crash the pipeline never selected: Socorro -> our tables -> scored, forced past the
    proto and stack dedups, WITHOUT the ordinary enqueue."""

    def setUp(self):
        self.added = []
        self.scored = []
        patches = [
            mock.patch.object(trigger.inspector, "get_crash_data", return_value=_processed()),
            mock.patch.object(models.Build, "get_id", return_value=7),
            mock.patch.object(models.Signature, "get_id", return_value=3),
            mock.patch.object(models.UUID, "add",
                              side_effect=lambda *a, **k: self.added.append((a, k)) or True),
            mock.patch.object(tools, "get_changeset", return_value="abc123def456"),
            mock.patch.object(update, "put_report",
                              side_effect=lambda *a, **k: self.scored.append((a, k)) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_happy_path(self):
        info = trigger.ingest(_UUID)
        self.assertEqual(info, {"channel": "release", "signature": "SplitSingleCharHelper",
                                "buildid": "20260903215306"})
        (args, kw), = self.added
        self.assertEqual(args[:2], (_UUID, 3))
        self.assertEqual(args[3], 7)
        self.assertTrue(kw["force"])
        (args, kw), = self.scored
        self.assertEqual((args[0], args[2], args[3], args[4], args[5]),
                         (_UUID, "release", "Firefox", "abc123def456", "SplitSingleCharHelper"))
        self.assertEqual(args[1], utils.get_build_date("20260903215306"))
        self.assertEqual((kw["enqueue"], kw["force"]), (False, True))

    def test_socorros_channel_becomes_our_label(self):
        self.assertEqual(trigger.channel_label("release"), "release")
        self.assertEqual(trigger.channel_label("aurora"), "beta")          # Developer Edition
        self.assertEqual(trigger.channel_label("esr", "153.1.0esr"), "esr153")
        self.assertIsNone(trigger.channel_label("esr", "115.40.0esr"))     # a retired line
        self.assertIsNone(trigger.channel_label("nightly-try"))
        self.assertIsNone(trigger.channel_label(None))

    def test_the_reasons_it_refuses(self):
        with mock.patch.object(trigger.inspector, "get_crash_data",
                               return_value=_processed(product="Thunderbird")):
            with self.assertRaisesRegex(trigger.TriggerError, "product 'Thunderbird'"):
                trigger.ingest(_UUID)
        with mock.patch.object(trigger.inspector, "get_crash_data",
                               return_value=_processed(release_channel="esr", version="115.40.0esr")):
            with self.assertRaisesRegex(trigger.TriggerError, "channel 'esr'"):
                trigger.ingest(_UUID)
        with mock.patch.object(models.Build, "get_id", return_value=None):
            with self.assertRaisesRegex(trigger.TriggerError, "not in the builds table"):
                trigger.ingest(_UUID)
        with mock.patch.object(update, "put_report", return_value=None):
            with self.assertRaisesRegex(trigger.TriggerError, "no usable stack"):
                trigger.ingest(_UUID)
        with mock.patch.object(trigger.inspector, "get_crash_data",
                               side_effect=RuntimeError("404")):
            with self.assertRaisesRegex(trigger.TriggerError, "no processed crash"):
                trigger.ingest(_UUID)
        self.assertEqual(self.scored, [])

    def test_trigger_one_ingests_then_queues(self):
        rt = _Retrigger()
        options = []
        with mock.patch.object(models.UUID, "exists", return_value=False), \
                mock.patch.object(models.Dossier, "set_run_options",
                                  side_effect=lambda u, o, commit=True: options.append(o) or True), \
                mock.patch.object(orchestrator, "retrigger_agent", rt):
            out = trigger.trigger_one(_UUID, file_bug=False, show_in_tasks=False)
        self.assertEqual((out["ok"], out["ingested"], out["channel"]), (True, True, "release"))
        self.assertEqual(rt.calls, [(_UUID, "release")])
        self.assertEqual((options[0]["autofile"], options[0]["show_in_tasks"]), (False, False))

    def test_a_refusal_is_reported_not_raised(self):
        with mock.patch.object(models.UUID, "exists", return_value=False), \
                mock.patch.object(models.Build, "get_id", return_value=None), \
                mock.patch.object(orchestrator, "retrigger_agent",
                                  side_effect=AssertionError("must not run")):
            out = trigger.trigger_one(_UUID)
        self.assertFalse(out["ok"])
        self.assertIn("builds table", out["error"])


class TestTheRunHonoursTheOptions(unittest.TestCase):
    def test_no_filing_is_the_first_gate(self):
        with mock.patch.object(models.Dossier, "run_options", return_value={"autofile": False}), \
                mock.patch.object(config, "autofile_channel_declared",
                                  side_effect=AssertionError("the channel gate ran first")):
            res = bugzilla_apply.autofile_bug(_UUID, {"channel": "nightly"}, [], {}, "culprit", 0.9)
        self.assertFalse(res["filed"])
        self.assertIn("filing disabled for this run", res["skipped"])

    def test_without_the_instruction_the_ordinary_gates_decide(self):
        for opts in ({}, {"autofile": True}, {"show_in_tasks": False}):
            with self.subTest(opts=opts), \
                    mock.patch.object(models.Dossier, "run_options", return_value=opts), \
                    mock.patch.object(config, "autofile_channel_declared", return_value=False):
                res = bugzilla_apply.autofile_bug(_UUID, {"channel": "nightly"}, [], {}, "culprit", 0.9)
            self.assertIn("no autofile configuration", res["skipped"])

    def test_the_options_survive_the_runs_own_settle_write(self):
        self.assertIn("run_options", models.Dossier._STICKY_PAYLOAD_KEYS)

    def test_hidden_runs_are_left_out_of_the_tasks_list(self):
        compiled = models.Dossier._list_tasks_query(5).statement.compile(dialect=postgresql.dialect())
        sql, binds = str(compiled), {str(v) for v in compiled.params.values()}
        self.assertIn("IS NULL", sql)          # absent key = shown
        self.assertIn("run_options", binds)    # the JSON path keys ride as bind parameters
        self.assertIn("show_in_tasks", binds)
        self.assertIn("false", binds)

    def test_reading_the_options_never_raises(self):
        with mock.patch.object(models.Dossier, "get_by_uuid", side_effect=RuntimeError("db")):
            self.assertEqual(models.Dossier.run_options(_UUID), {})
        with mock.patch.object(models.Dossier, "get_by_uuid", return_value=None):
            self.assertEqual(models.Dossier.run_options(_UUID), {})


class TestPutReportFlags(unittest.TestCase):
    """`enqueue=False` scores without firing the agent; `force` stores a stack the build has
    already seen. Both are what the trigger's own forced run needs."""

    def setUp(self):
        self.frames = []
        self.enqueued = []
        patches = [
            mock.patch.object(update.inspector, "get_crash", return_value={"nonjava": {"hash": "h1"}}),
            mock.patch.object(models.Changeset, "to_analyze", return_value=[]),
            mock.patch.object(models.UUID, "is_stackhash_existing", return_value=True),
            mock.patch.object(models.CrashStack, "put_frames",
                              side_effect=lambda *a, **k: self.frames.append(a[0])),
            mock.patch.object(models.UUID, "add_stack_hash"),
            mock.patch.object(models.UUID, "set_analyzed"),
            mock.patch.object(update, "rising_rate_mindate", side_effect=lambda m, *a: m),
            mock.patch.object(orchestrator, "enqueue_agent",
                              side_effect=lambda *a, **k: self.enqueued.append(a)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.bid = utils.get_build_date("20260906093052")

    def test_forced_and_not_enqueued(self):
        out = update.put_report(_UUID, self.bid, "nightly", "Firefox", "abc", "sig",
                                enqueue=False, force=True)
        self.assertTrue(out)
        self.assertEqual(self.frames, [_UUID])        # stored despite the known hash
        self.assertEqual(self.enqueued, [])

    def test_the_ordinary_path_is_unchanged(self):
        out = update.put_report(_UUID, self.bid, "nightly", "Firefox", "abc", "sig")
        self.assertFalse(out)                          # a known stack: nothing new to read
        self.assertEqual(self.frames, [])
        self.assertEqual(self.enqueued, [])
        with mock.patch.object(models.UUID, "is_stackhash_existing", return_value=False):
            out = update.put_report(_UUID, self.bid, "nightly", "Firefox", "abc", "sig")
        self.assertTrue(out)
        self.assertEqual(self.enqueued, [(_UUID, "nightly")])

    def test_no_dump_is_none(self):
        with mock.patch.object(update.inspector, "get_crash", return_value=None):
            self.assertIsNone(update.put_report(_UUID, self.bid, "nightly", "Firefox", "abc"))


if __name__ == "__main__":
    unittest.main()
