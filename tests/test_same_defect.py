# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Same-defect check: the agent's prompt, options and parsing, the lookup of bugs attributed to
# the regressor, and the filer's use of the answer. No live agent or network calls.
#   DATABASE_URL=sqlite:// python -m unittest tests.test_same_defect
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, report_bug  # noqa: E402
from crashclouseau.agent import same_defect  # noqa: E402
from tests.test_autofile import _Base, _INFO, _is_postgres  # noqa: E402

_DOSSIER = {
    "candidate": {"node": "85ddbfbd3a62", "bug": 42},
    "verdict": {"title": "Recursion in Foo::Layout",
                "mechanism": {"statement": "The change removed a guard in Foo::Layout."}},
    "data_flow": {"summary": "Foo::Layout calls Foo::Measure again."},
    "crash": {"signature": "Foo::Bar", "moz_crash_reason": "",
              "frames": [{"stackpos": 0, "function": "Foo::Brief"}]},
}
_STACK = {"frames": [{"stackpos": 0, "function": "Foo::Bar", "filename": "foo.cpp", "line": 12},
                     {"stackpos": 1, "function": "Foo::Layout", "filename": "foo.cpp",
                      "line": 40}]}
_SD_CFG = {"enabled": True, "model": "claude-opus-5-5", "effort": "medium", "max_turns": 20,
           "min_confidence": "medium", "max_bugs": 5}


_BUILDID = "20260903215306"


def _row(bid, resolution="", status="NEW", sigs=("Other::Sig",), created="2026-09-20T00:00:00Z",
         product="Core", keywords=(), dupe_of=None, summary="Crash in something", resolved=None):
    return {"id": bid, "summary": summary, "status": status, "resolution": resolution,
            "dupe_of": dupe_of, "creation_time": created, "product": product,
            "keywords": list(keywords), "cf_last_resolved": resolved,
            "cf_crash_signature": " ".join("[@ {}]".format(s) for s in sigs)}


class TestPrompt(unittest.TestCase):
    def test_crash_from_dossier(self):
        crash = same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, _STACK["frames"])
        self.assertEqual(crash["title"], "Recursion in Foo::Layout")
        self.assertEqual(crash["mechanism"], "The change removed a guard in Foo::Layout.")
        self.assertEqual(crash["data_flow"], "Foo::Layout calls Foo::Measure again.")
        self.assertEqual(crash["frames"][1]["function"], "Foo::Layout")

    def test_the_ipc_fatal_error_message_reaches_the_prompt(self):
        msg = "SessionHistoryInfo with invalid shared state identifier"
        crash = same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, None, msg)
        self.assertEqual(crash["ipc_fatal_error_msg"], msg)
        self.assertEqual(
            same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, None)["ipc_fatal_error_msg"], "")
        sib = {"bug": 7, "summary": "s", "title": "t", "ipc_fatal_error_msg": "Other check"}
        text = same_defect.user_prompt(crash, {"bug": 42}, [sib])
        self.assertIn("IPC FatalError message: " + msg, text)
        self.assertIn("IPC FatalError message: Other check", text)

    def test_frames_fall_back_to_the_dossier(self):
        crash = same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, None)
        self.assertEqual(crash["frames"], [{"stackpos": 0, "function": "Foo::Brief"}])

    def test_user_prompt_shows_each_bug(self):
        crash = same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, _STACK["frames"])
        ours = {"bug": 7, "summary": "Crash in [@ A]", "status": "NEW", "signatures": ["A"],
                "title": "Recursion via A", "mechanism": "A recurses.",
                "frames": [{"stackpos": 0, "function": "A::Run"}]}
        theirs = {"bug": 8, "summary": "Crash in [@ B]", "status": "RESOLVED FIXED",
                  "signatures": ["B", "C"], "description": "x" * 5000}
        text = same_defect.user_prompt(crash, {"bug": 42, "node": "85ddbfbd3a62"},
                                       [ours, theirs])
        self.assertIn("Regressor: bug 42, changeset 85ddbfbd3a62", text)
        self.assertIn("Signature: Foo::Bar", text)
        self.assertIn("1 Foo::Layout  (foo.cpp:40)", text)
        self.assertIn("BUG 7: Crash in [@ A]", text)
        self.assertIn("Mechanism: A recurses.", text)
        self.assertIn("0 A::Run", text)
        self.assertIn("Status: RESOLVED FIXED", text)
        self.assertIn("Crash signatures: [@ B] [@ C]", text)
        self.assertIn("[... truncated]", text)
        self.assertNotIn("x" * 4001, text)
        self.assertIn("mcp__patch__diff 85ddbfbd3a62", text)

    def test_later_comments_newest_kept(self):
        comments = [{"author": "a@moz", "text": "old " + "y" * 3000},
                    *[{"author": "b@moz", "text": "z" * 1500}] * 3,
                    {"author": "c@moz", "text": "The deadlock is the same."}]
        lines = same_defect._comment_lines(comments)
        self.assertEqual(lines[0], "Later comments:")
        self.assertTrue(lines[-1].startswith("- c@moz: The deadlock"))
        self.assertIn("[... truncated]", lines[-2])
        self.assertFalse(any("old" in x for x in lines))
        self.assertEqual(same_defect._comment_lines([]), [])

    def test_product_is_named(self):
        self.assertIn("whether a Firefox crash", same_defect._system_prompt())
        self.assertIn("whether a Fenix crash", same_defect._system_prompt("Fenix"))


class TestOptions(unittest.TestCase):
    def test_scoped_tools_only(self):
        with mock.patch.object(config, "get_agent_same_defect", return_value=_SD_CFG):
            opts = same_defect.build_options("nightly", "abc", searchfox_client=object())
        self.assertEqual(opts.tools, [])
        self.assertEqual(set(opts.mcp_servers), {"searchfox", "patch", "source"})
        allowed = set(opts.allowed_tools)
        self.assertIn("mcp__patch__diff", allowed)
        self.assertIn("mcp__source__raw_file", allowed)
        for banned in ("Bash", "Read", "Task", "mcp__bugzilla__bug"):
            self.assertNotIn(banned, allowed)
        self.assertEqual((opts.model, opts.effort, opts.max_turns),
                         ("claude-opus-5-5", "medium", 20))

    def test_config_defaults(self):
        with mock.patch.object(config, "get_agent", return_value={}):
            cfg = config.get_agent_same_defect()
        self.assertFalse(cfg["enabled"])
        self.assertEqual((cfg["min_confidence"], cfg["max_bugs"]), ("medium", 5))


class TestParse(unittest.TestCase):
    def test_a_listed_bug(self):
        text = ('...\n```json\n{"same_defect_bug": 7, "confidence": "high", '
                '"reason": "Same recursion."}\n```')
        out = same_defect.parse_same_defect(text, [7, 8])
        self.assertEqual((out.bug, out.confidence, out.reason), (7, "high", "Same recursion."))

    def test_null(self):
        text = '```json\n{"same_defect_bug": null, "confidence": "medium", "reason": "No."}\n```'
        self.assertIsNone(same_defect.parse_same_defect(text, [7]).bug)

    def test_an_unlisted_bug_is_no_match(self):
        text = '```json\n{"same_defect_bug": "#9", "confidence": "high", "reason": ""}\n```'
        self.assertIsNone(same_defect.parse_same_defect(text, [7]).bug)

    def test_unknown_confidence_is_low(self):
        text = '```json\n{"same_defect_bug": 7, "confidence": "sure", "reason": ""}\n```'
        self.assertEqual(same_defect.parse_same_defect(text, [7]).confidence, "low")

    def test_missing_block(self):
        self.assertIsNone(same_defect.parse_same_defect("no json here", [7]))
        self.assertIsNone(same_defect.parse_same_defect(
            '```json\n{"same_defect_bug": "seven"}\n```', [7]))


class TestFilingAnalysis(unittest.TestCase):
    def test_the_filing_carries_its_ipc_fatal_error_message(self):
        row = mock.Mock(payload={"dossier": _DOSSIER})
        with mock.patch.object(bugzilla_apply.models.Dossier, "get_by_uuid", return_value=row), \
                mock.patch.object(bugzilla_apply.models.CrashStack, "get_by_uuid",
                                  return_value=(_STACK, {})), \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"ipc_fatal_error_msg": "Bad id"}) as reason:
            out = bugzilla_apply._filing_analysis({"uuid": "u-7", "signature": "A"})
        reason.assert_called_once_with("u-7")
        self.assertEqual(out["ipc_fatal_error_msg"], "Bad id")
        self.assertEqual(out["title"], "Recursion in Foo::Layout")


class TestSameRegressorBugs(unittest.TestCase):
    def setUp(self):
        p = [
            mock.patch.object(bugzilla_apply.models.Dossier, "filings_for_regressor",
                              return_value=[{"uuid": "u-7", "bug": 7, "signature": "A",
                                             "mode": "new_bug"}]),
            mock.patch.object(bugzilla_apply, "_regression_ids", return_value=[7, 8]),
            mock.patch.object(bugzilla_apply, "_bug_comments",
                              return_value=("comment 0", [{"author": "dev@moz.example",
                                                           "text": "Same deadlock."}])),
            mock.patch.object(bugzilla_apply, "_filing_analysis",
                              return_value={"title": "Ours", "mechanism": "m",
                                            "data_flow": "", "crash_reason": "",
                                            "frames": []}),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def _run(self, rows, extra=(), signature="Foo::Bar", max_bugs=5, buildid=_BUILDID):
        def fetch(ids, timeout=None):
            return [r for r in list(rows) + list(extra) if r["id"] in ids]
        with mock.patch.object(bugzilla_apply, "_crash_bug_rows", side_effect=fetch):
            return bugzilla_apply._same_regressor_bugs(42, signature, "Firefox", max_bugs,
                                                       buildid=buildid)

    def test_ours_with_analysis_and_theirs_with_comment_0(self):
        out = self._run([_row(7, sigs=("A",), created="2026-09-21T00:00:00Z"),
                         _row(8, resolution="FIXED", status="RESOLVED", sigs=("B",),
                              resolved="2026-09-09T21:13:48Z")])
        self.assertEqual([b["bug"] for b in out], [7, 8])
        self.assertEqual((out[0]["title"], out[0]["signatures"]), ("Ours", ["A"]))
        self.assertNotIn("description", out[0])
        self.assertEqual((out[1]["description"], out[1]["status"]),
                         ("comment 0", "RESOLVED FIXED"))
        self.assertEqual(out[0]["comments"], [{"author": "dev@moz.example",
                                               "text": "Same deadlock."}])

    def test_filters(self):
        with mock.patch.object(bugzilla_apply, "_regression_ids", return_value=[8, 9, 10, 11, 12]):
            out = self._run([
                _row(7, resolution="WONTFIX", status="RESOLVED"),
                _row(8, keywords=("meta",)),
                _row(9, sigs=()),
                _row(10, sigs=("Foo::Bar",)),
                _row(11, product="Thunderbird"),
                _row(12),
            ])
        self.assertEqual([b["bug"] for b in out], [12])

    def test_fixed_candidates_require_resolution_after_build(self):
        fixed = dict(resolution="FIXED", status="RESOLVED", sigs=("B",))
        rows = [_row(8, resolved="2026-09-09T21:13:48Z", **fixed),
                _row(9, resolved="2026-09-01T13:27:26Z", **fixed),
                _row(10, **fixed)]
        with mock.patch.object(bugzilla_apply, "_regression_ids", return_value=[8, 9, 10]):
            self.assertEqual([b["bug"] for b in self._run(rows)], [8])
            self.assertEqual(self._run(rows, buildid=None), [])
            dt = bugzilla_apply.datetime(2026, 9, 3, 21, 53, 6,
                                         tzinfo=bugzilla_apply.timezone.utc)
            self.assertEqual([b["bug"] for b in self._run(rows, buildid=dt)], [8])

    def test_a_failed_comment_read_is_a_failed_lookup(self):
        with mock.patch.object(bugzilla_apply, "_bug_comments", return_value=None):
            self.assertIsNone(self._run([_row(7, sigs=("A",))]))

    def test_a_duplicate_is_replaced_by_its_target(self):
        out = self._run([_row(7, resolution="DUPLICATE", status="RESOLVED", dupe_of=99)],
                        extra=[_row(99, sigs=("Z",))])
        self.assertEqual([b["bug"] for b in out], [99])
        self.assertEqual(out[0]["description"], "comment 0")

    def test_newest_first_and_capped(self):
        rows = [_row(i, created="2026-09-{:02d}T00:00:00Z".format(i)) for i in range(10, 20)]
        with mock.patch.object(bugzilla_apply, "_regression_ids",
                               return_value=list(range(10, 20))):
            out = self._run(rows, max_bugs=3)
        self.assertEqual([b["bug"] for b in out], [19, 18, 17])

    def test_a_failed_lookup_is_none(self):
        with mock.patch.object(bugzilla_apply, "_regression_ids", return_value=None):
            self.assertIsNone(self._run([_row(7)]))
        with mock.patch.object(bugzilla_apply.models.Dossier, "filings_for_regressor",
                               return_value=None):
            self.assertIsNone(self._run([_row(7)]))
        with mock.patch.object(bugzilla_apply, "_crash_bug_rows", return_value=None):
            self.assertIsNone(bugzilla_apply._same_regressor_bugs(42, "Foo::Bar", "Firefox", 5))


class TestBugComments(unittest.TestCase):
    def test_automation_is_left_out(self):
        resp = mock.Mock()
        resp.json.return_value = {"bugs": {"7": {"comments": [
            {"creator": "dev@moz.example", "text": "comment 0"},
            {"creator": "release-mgmt-account-bot@mozilla.tld", "text": "bot"},
            {"creator": "phab-bot@bmo.tld", "text": "patch"},
            {"creator": "clouseau-bot@mozilla.com", "text": "ours"},
            {"creator": "jdoe@moz.example", "text": "Same deadlock."}]}}}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=resp):
            first, later = bugzilla_apply._bug_comments(7)
        self.assertEqual(first, "comment 0")
        self.assertEqual(later, [{"author": "jdoe@moz.example", "text": "Same deadlock."}])

    def test_a_failed_read_is_none(self):
        with mock.patch.object(bugzilla_apply.net, "get", side_effect=RuntimeError("503")):
            self.assertIsNone(bugzilla_apply._bug_comments(7))


class TestSameDefectBug(unittest.TestCase):
    _BUGS = [{"bug": 7, "summary": "s", "status": "NEW", "signatures": ["A"]}]

    def _run(self, answer, bugs=_BUGS, dossier=_DOSSIER, cfg=_SD_CFG):
        async def run(*a, **k):
            return answer
        with mock.patch.object(bugzilla_apply, "_same_regressor_bugs",
                               return_value=bugs) as lookup, \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"ipc_fatal_error_msg": "Bad id"}) as reason, \
                mock.patch.object(same_defect, "run_same_defect", side_effect=run) as agent:
            out = bugzilla_apply._same_defect_bug("u-1", dict(_INFO, node="abc",
                                                              buildid=_BUILDID),
                                                  _STACK, dossier, "Foo::Bar", cfg)
        if agent.called:
            reason.assert_called_once_with("u-1")
        if dossier.get("candidate", {}).get("bug"):
            self.assertEqual(lookup.call_args.kwargs["buildid"], _BUILDID)
        return out, agent

    def test_a_match(self):
        (bug, check), agent = self._run(same_defect.SameDefect(bug=7, confidence="medium",
                                                               reason="Same."))
        self.assertEqual(bug, 7)
        self.assertEqual((check["regressor"], check["bugs"], check["reason"]), (42, [7], "Same."))
        args, kwargs = agent.call_args
        self.assertEqual(args[0]["ipc_fatal_error_msg"], "Bad id")
        self.assertEqual(args[1], {"bug": 42, "node": "85ddbfbd3a62"})
        self.assertEqual(kwargs["build_rev"], "abc")

    def test_below_the_floor(self):
        (bug, check), _ = self._run(same_defect.SameDefect(bug=7, confidence="low"))
        self.assertIsNone(bug)
        self.assertEqual(check["bug"], 7)

    def test_no_match(self):
        (bug, check), _ = self._run(same_defect.SameDefect(bug=None, confidence="high"))
        self.assertIsNone(bug)
        self.assertIsNone(check["bug"])

    def test_no_answer(self):
        (bug, check), _ = self._run(None)
        self.assertIsNone(bug)
        self.assertEqual(check["error"], "no usable answer")

    def test_nothing_to_compare(self):
        (bug, check), agent = self._run(None, bugs=[])
        self.assertEqual((bug, check), (None, None))
        agent.assert_not_called()
        (bug, check), agent = self._run(None, dossier={"candidate": {"node": "n"}})
        self.assertEqual((bug, check), (None, None))
        agent.assert_not_called()

    def test_a_failed_lookup(self):
        (bug, check), agent = self._run(None, bugs=None)
        self.assertIsNone(bug)
        self.assertEqual(check["error"], "bug lookup failed")
        agent.assert_not_called()


class TestTheFiler(_Base):
    """``autofile_bug`` with the check enabled."""

    _CHECK = {"regressor": 42, "bugs": [7], "bug": 7, "confidence": "medium",
              "reason": "Same recursion.", "cost_usd": 0.05}

    def setUp(self):
        super().setUp()
        self.errors = []
        p = [
            mock.patch.object(bugzilla_apply.config, "get_agent_same_defect",
                              return_value=dict(_SD_CFG)),
            mock.patch.object(bugzilla_apply, "_same_defect_bug",
                              return_value=(7, dict(self._CHECK))),
            mock.patch.object(bugzilla_apply, "_signature_field", return_value="[@ A]"),
            mock.patch.object(bugzilla_apply.models.Dossier, "record_filing_error",
                              side_effect=lambda u, i: self.errors.append(i) or True),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_the_signature_goes_onto_the_same_defect_bug(self):
        res = self._file(dossier=_DOSSIER)
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"], res["needinfo"]), (7, "same_defect", None))
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [])
        [(bug, changes)] = self.puts
        self.assertEqual(bug, 7)
        self.assertEqual(changes["cf_crash_signature"], "[@ A]\n[@ Foo::Bar]")
        body = changes["comment"]["body"]
        self.assertIn("Adding `[@ Foo::Bar]`", body)
        self.assertIn("same regressor (bug 42)", body)
        self.assertIn("Same recursion.", body)
        self.assertIn("Crash report: https://crash-stats.mozilla.org/report/index/u-1", body)
        self.assertEqual(self.filed, [("u-1", res)])
        self.assertEqual(res["same_defect"]["regressor"], 42)

    def test_a_skip_channel_declines(self):
        res = self._file(dossier=_DOSSIER, comment_on_existing="skip")
        self.assertFalse(res["filed"])
        self.assertEqual(res["bug"], 7)
        self.assertIn("same defect", res["skipped"])
        self.assertEqual((self.puts, self.created, self.filed), ([], [], []))

    def test_a_failed_write_is_recorded(self):
        bugzilla_apply._put_bug.side_effect = RuntimeError("503")
        res = self._file(dossier=_DOSSIER)
        self.assertFalse(res["filed"])
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertEqual(self.errors[0]["mode"], "same_defect")
        self.assertEqual((self.created, self.filed), ([], []))

    def test_a_signature_already_there_writes_nothing(self):
        bugzilla_apply._signature_field.return_value = "[@ A]\n[@ Foo::Bar]"
        res = self._file(dossier=_DOSSIER)
        self.assertFalse(res["filed"])
        self.assertIn("already carries this signature", res["skipped"])
        self.assertEqual((self.puts, self.created), ([], []))

    def test_no_match_files_a_new_bug_and_records_the_check(self):
        check = dict(self._CHECK, bug=None)
        bugzilla_apply._same_defect_bug.return_value = (None, check)
        res = self._file(dossier=_DOSSIER)
        self.assertEqual((res["filed"], res["mode"]), (True, "new_bug"))
        self.assertEqual(res["same_defect_check"], check)
        self.assertEqual(len(self.created), 1)

    def test_disabled(self):
        bugzilla_apply.config.get_agent_same_defect.return_value = dict(_SD_CFG, enabled=False)
        res = self._file(dossier=_DOSSIER)
        self.assertEqual(res["mode"], "new_bug")
        bugzilla_apply._same_defect_bug.assert_not_called()
        self.assertNotIn("same_defect_check", res)

    def test_not_for_a_withheld_crash(self):
        with mock.patch.object(bugzilla_apply.sensitive, "is_withheld", return_value=True):
            self._file(dossier=_DOSSIER)
        bugzilla_apply._same_defect_bug.assert_not_called()

    def test_not_when_an_open_bug_is_the_venue(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[{"id": 1234, "creation_time": "2026-08-01T00:00:00Z",
                                              "product": "Core", "keywords": [],
                                              "regressed_by": []}]):
            res = self._file(dossier=_DOSSIER)
        self.assertEqual(res["mode"], "comment_on_existing")
        bugzilla_apply._same_defect_bug.assert_not_called()


@unittest.skipUnless(_is_postgres(), "the filed_bug JSONB queries need a disposable Postgres")
class TestFilingsForRegressor(unittest.TestCase):
    def test_our_filings_for_a_regressor(self):
        from datetime import datetime, timezone
        from crashclouseau import db, models
        models.create()
        build = models.Build(datetime(2026, 9, 23, 21, 39, 29, tzinfo=timezone.utc), "Firefox",
                             "nightly", "157.0a1", None)
        db.session.add(build)
        db.session.commit()
        self.addCleanup(lambda: (db.session.rollback(), db.session.delete(build),
                                 db.session.commit()))
        sigid = models.Signature.get_id("Same::Defect")
        for name, cand, filed in (("sd-1", 42, True), ("sd-2", 42, False), ("sd-3", 43, True)):
            db.session.add(models.UUID(name, sigid, name, build.id))
            db.session.commit()
            models.Dossier.upsert(name, payload={"dossier": {"candidate": {"bug": cand}}},
                                  status="done")
            models.Dossier.record_filed_bug(name, {"filed": filed, "bug": 7, "mode": "new_bug",
                                                   "signature": "Same::Defect"})
        self.assertEqual(models.Dossier.filings_for_regressor(42),
                         [{"uuid": "sd-1", "bug": 7, "signature": "Same::Defect",
                           "mode": "new_bug"}])
        self.assertEqual(models.Dossier.filings_for_regressor(None), [])


if __name__ == "__main__":
    unittest.main()
