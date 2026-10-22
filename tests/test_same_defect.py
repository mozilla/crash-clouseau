# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Same-defect prompts, candidate selection and both filers, with mocked agent/network calls.
#   DATABASE_URL=sqlite:// python -m unittest tests.test_same_defect
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, report_bug, spikes  # noqa: E402
from crashclouseau.agent import same_defect, spike_escalation as se  # noqa: E402
from crashclouseau.agent.spike_agent import SpikeFindings  # noqa: E402
from tests.test_autofile import _Base, _INFO, _bug, _cfg, _is_postgres  # noqa: E402
from tests.test_release_filing import _RELEASE_INFO, _upstream  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

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
           "min_confidence": "medium", "max_bugs": 5,
           "lookback_days": 30, "stack_depth": 15}


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

    def test_without_a_regressor_the_prompt_asks_for_the_stacks_only(self):
        crash = same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, _STACK["frames"])
        sib = {"bug": 7, "summary": "Crash in [@ A]", "signatures": ["A"], "description": "d",
               "shared_frames": ["Foo::Layout", "Foo::Measure"]}
        text = same_defect.user_prompt(crash, None, [sib])
        self.assertNotIn("Regressor:", text)
        self.assertNotIn("mcp__patch__diff", text)
        self.assertIn("Inspect the code the stacks go through", text)
        self.assertIn("Listed because: our filing, whose stack shares these frames with the "
                      "crash: Foo::Layout; Foo::Measure", text)
        self.assertNotIn("attributed to the regressor", text)
        text = same_defect.user_prompt(crash, {"bug": 42}, [dict(sib, by_regressor=True)])
        self.assertIn("Listed because: attributed to the regressor", text)

    def test_crash_from_spike(self):
        findings = SpikeFindings(summary="The key generation fails.", trigger_path="Init path.")
        details = {"moz_crash_reason": "MOZ_RELEASE_ASSERT(false)", "ipc_fatal_error_msg": "m"}
        for f in (findings, findings.model_dump()):
            crash = same_defect.crash_from_spike("A", f, _STACK["frames"], details)
            self.assertEqual((crash["mechanism"], crash["data_flow"], crash["crash_reason"],
                              crash["ipc_fatal_error_msg"], crash["title"]),
                             ("The key generation fails.", "Init path.",
                              "MOZ_RELEASE_ASSERT(false)", "m", ""))
        self.assertEqual(same_defect.crash_from_spike("A", None, None)["mechanism"], "")

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


def _f(function, filename="f.cpp"):
    return {"function": function, "filename": filename}


class TestFrames(unittest.TestCase):
    def test_a_frame_key_drops_arguments_and_lambda_spellings(self):
        self.assertEqual(same_defect.frame_key(_f("Foo::Bar(int, char const*)")),
                         ("f.cpp", "Foo::Bar"))
        self.assertEqual(same_defect.frame_key(_f("Foo::<T>::operator()(int) const")),
                         ("f.cpp", "Foo::<T>::operator()"))
        self.assertEqual(same_defect.frame_key(_f("Foo::{lambda#1}::operator()")),
                         same_defect.frame_key(_f("Foo::<T>::operator()")))
        # Distinguish Java methods with the same name in different files.
        self.assertNotEqual(same_defect.frame_key(_f("invoke", "relay/FxRelay.kt")),
                            same_defect.frame_key(_f("invoke", "fxa/FirefoxAccount.kt")))

    def test_frame_keys_keep_the_first_position(self):
        frames = [_f("A(int)"), _f("B"), _f("A(char)"), {"function": ""}, _f("C")]
        self.assertEqual(same_defect.frame_keys(frames, 3),
                         {("f.cpp", "A"): 0, ("f.cpp", "B"): 1})
        self.assertEqual(same_defect.frame_keys(None, 3), {})

    def test_generic_frames_are_ignored_and_rare_ones_rank_first(self):
        def item(name, *functions):
            return {"name": name, "keys": same_defect.frame_keys([_f(x) for x in functions], 15)}
        # "Loop" is in 7 of 21 filings: generic. "Rare" is in one, "Mid" in three.
        corpus = [item("loop{}".format(i), "Loop") for i in range(4)]
        corpus += [item("other{}".format(i), "Other{}".format(i)) for i in range(13)]
        corpus += [item("rare", "Rare", "Loop"), item("mid", "Mid", "Loop"),
                   item("mid2", "Mid", "Mid2", "Loop"), item("mid3", "Mid")]
        keys = same_defect.frame_keys([_f("Mid"), _f("Rare"), _f("Loop")], 15)
        ranked = same_defect.rank_by_shared_frames(keys, corpus, corpus)
        self.assertEqual([x[2]["name"] for x in ranked], ["rare", "mid", "mid2", "mid3"])
        self.assertEqual(ranked[0][1], [("f.cpp", "Rare")])
        self.assertEqual(same_defect.rank_by_shared_frames({}, corpus, corpus), [])


class TestFilingAnalysis(unittest.TestCase):
    def test_a_withheld_analysis_stays_out_of_a_public_comparison(self):
        esc = mock.Mock(payload={"findings": {"summary": "s"},
                                 "filing": {"screened": True, "findings_withheld": True}})
        row = mock.Mock(payload={"dossier": _DOSSIER, "filed_bug": {"withdrawn_refs": 1}})
        with mock.patch.object(bugzilla_apply.models.SpikeEscalation, "get", return_value=esc), \
                mock.patch.object(bugzilla_apply.models.Dossier, "get_by_uuid", return_value=row), \
                mock.patch.object(bugzilla_apply.models.CrashStack, "get_by_uuid",
                                  return_value=(_STACK, {})), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}):
            for filing in ({"uuid": "u-7", "escalation": 12}, {"uuid": "u-7"}):
                self.assertEqual(bugzilla_apply._filing_analysis(filing, public=True), {})
                self.assertTrue(bugzilla_apply._filing_analysis(filing)["mechanism"])

    def test_a_spike_filing_reads_the_escalation_findings(self):
        esc = mock.Mock(payload={"findings": {"summary": "Key generation fails.",
                                              "trigger_path": "Init."}})
        with mock.patch.object(bugzilla_apply.models.SpikeEscalation, "get",
                               return_value=esc) as get, \
                mock.patch.object(bugzilla_apply.models.Dossier, "get_by_uuid") as dossier, \
                mock.patch.object(bugzilla_apply.models.CrashStack, "get_by_uuid",
                                  return_value=(_STACK, {})), \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"moz_crash_reason": "MOZ_CRASH(x)"}):
            out = bugzilla_apply._filing_analysis({"uuid": "u-7", "signature": "A",
                                                   "escalation": 12})
            get.assert_called_once_with(12)
            dossier.assert_not_called()
            self.assertEqual((out["mechanism"], out["data_flow"], out["crash_reason"]),
                             ("Key generation fails.", "Init.", "MOZ_CRASH(x)"))
            self.assertEqual(out["frames"], _STACK["frames"])
            esc.payload = {"findings": {}}
            self.assertEqual(bugzilla_apply._filing_analysis(
                {"uuid": "u-7", "signature": "A", "escalation": 12}), {})

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


class TestCandidates(unittest.TestCase):
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
            mock.patch.object(bugzilla_apply, "_own_filing_records", return_value=[]),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_a_signature_removed_from_our_bug_is_not_added_back(self):
        records = [{"uuid": "u-30", "bug": 30, "signature": "Foo::$", "mode": "new_bug"},
                   {"uuid": "u-7", "bug": 7, "signature": "Foo::{lambda#1}", "mode": "new_bug"},
                   {"uuid": "u-40", "bug": 40, "signature": "Foo::<T>", "mode": "same_defect"},
                   {"uuid": "u-8", "bug": 8, "signature": "Foo::Other", "mode": "new_bug"}]
        rows = [_row(7, sigs=("A",)), _row(8, sigs=("B",)), _row(30, sigs=("C",)),
                _row(40, resolution="DUPLICATE", status="RESOLVED", dupe_of=99)]
        with mock.patch.object(bugzilla_apply, "_own_filing_records", return_value=records):
            out = self._run(rows, extra=[_row(99, sigs=("Z",))], signature="Foo::<T>",
                            frames=_STACK["frames"], ranked=self._ranked(30, 40))
        # Exclude 7 and 30; duplicate target 99 has no prior filing for this signature.
        self.assertEqual([b["bug"] for b in out], [8, 99])

    def test_a_failed_record_lookup_is_a_failed_lookup(self):
        with mock.patch.object(bugzilla_apply, "_own_filing_records", return_value=None):
            self.assertIsNone(self._run([_row(7)]))

    def _run(self, rows, extra=(), signature="Foo::Bar", max_bugs=5, buildid=_BUILDID,
             hidden=(), token=None, regressor=42, frames=None, ranked=()):
        def fetch(ids, timeout=None, token=None):
            visible = [r for r in list(rows) + list(extra) if r["id"] not in hidden or token]
            return [r for r in visible if r["id"] in ids]
        with mock.patch.object(bugzilla_apply, "_crash_bug_rows", side_effect=fetch), \
                mock.patch.object(bugzilla_apply, "_own_filings_sharing_frames",
                                  return_value=list(ranked) if ranked is not None else None):
            return bugzilla_apply._same_defect_candidates(
                regressor, signature, "Firefox", max_bugs, buildid=buildid, token=token,
                frames=frames, cfg=_SD_CFG)

    @staticmethod
    def _ranked(*bugs):
        return [(10.0 - i, ["Shared::Frame{}".format(b)],
                 {"uuid": "s-{}".format(b), "bug": b, "signature": "S{}".format(b),
                  "mode": "spike_new_bug", "escalation": b})
                for i, b in enumerate(bugs)]

    def test_our_filings_sharing_frames_follow_the_regressor_bugs(self):
        rows = [_row(7, sigs=("A",)), _row(8, sigs=("B",)), _row(30, sigs=("C",)),
                _row(31, sigs=("D",))]
        with mock.patch.object(bugzilla_apply, "_filing_analysis",
                               side_effect=lambda f, public: {"title": f["signature"]}) as analysis:
            out = self._run(rows, frames=_STACK["frames"], ranked=self._ranked(31, 30, 7))
        self.assertEqual([b["bug"] for b in out], [7, 8, 31, 30])
        self.assertEqual([b.get("by_regressor") for b in out], [True, True, None, None])
        self.assertEqual([b.get("shared_frames") for b in out],
                         [["Shared::Frame7"], None, ["Shared::Frame31"], ["Shared::Frame30"]])
        # Prefer the regressor filing on bug 7; use spike analyses for 31 and 30.
        self.assertEqual([(c.args[0]["uuid"], c.kwargs["public"])
                          for c in analysis.call_args_list],
                         [("u-7", True), ("s-31", True), ("s-30", True)])
        self.assertEqual(out[2]["title"], "S31")
        self.assertNotIn("description", out[2])

    def test_without_a_regressor_only_our_filings_sharing_frames(self):
        rows = [_row(30, sigs=("C",)),
                _row(31, sigs=("D",), resolution="WONTFIX", status="RESOLVED")]
        with mock.patch.object(bugzilla_apply.models.Dossier, "filings_for_regressor") as ours, \
                mock.patch.object(bugzilla_apply, "_regression_ids") as listed:
            out = self._run(rows, regressor=None, frames=_STACK["frames"],
                            ranked=self._ranked(31, 30))
            ours.assert_not_called()
            listed.assert_not_called()
        self.assertEqual([b["bug"] for b in out], [30])
        self.assertEqual(self._run(rows, regressor=None), [])

    def test_a_duplicate_of_our_filing_keeps_its_shared_frames(self):
        out = self._run([_row(30, resolution="DUPLICATE", status="RESOLVED", dupe_of=99)],
                        extra=[_row(99, sigs=("Z",))], regressor=None, frames=_STACK["frames"],
                        ranked=self._ranked(30))
        self.assertEqual([(b["bug"], b["shared_frames"]) for b in out],
                         [(99, ["Shared::Frame30"])])
        self.assertEqual(out[0]["description"], "comment 0")

    def test_the_cap_counts_both_sources(self):
        rows = [_row(7, sigs=("A",)), _row(8, sigs=("B",)), _row(30, sigs=("C",))]
        out = self._run(rows, max_bugs=2, frames=_STACK["frames"], ranked=self._ranked(30))
        self.assertEqual([b["bug"] for b in out], [7, 8])

    def test_a_failed_filing_lookup_is_a_failed_lookup(self):
        self.assertIsNone(self._run([_row(7)], frames=_STACK["frames"], ranked=None))

    def test_our_restricted_filing_is_read_with_the_token(self):
        rows = [_row(7, sigs=("A",)), _row(8, sigs=("B",))]
        with mock.patch.object(bugzilla_apply, "_bug_comments",
                               return_value=("comment 0", [])) as comments:
            out = self._run(rows, hidden={7}, token="tok")
        self.assertEqual(sorted(b["bug"] for b in out), [7, 8])
        tokens = {c.args[0]: c.kwargs.get("token") for c in comments.call_args_list}
        self.assertEqual(tokens, {7: "tok", 8: None})
        self.assertEqual({b["bug"]: b.get("restricted") for b in out}, {7: True, 8: None})

    def test_without_a_token_our_restricted_filing_stays_out(self):
        out = self._run([_row(7, sigs=("A",)), _row(8, sigs=("B",))], hidden={7})
        self.assertEqual([b["bug"] for b in out], [8])

    def test_ours_with_analysis_and_theirs_with_comment_0(self):
        out = self._run([_row(7, sigs=("A",), created="2026-09-21T00:00:00Z"),
                         _row(8, resolution="FIXED", status="RESOLVED", sigs=("B",),
                              resolved="2026-09-09T21:13:48Z")])
        self.assertEqual([b["bug"] for b in out], [7, 8])
        self.assertEqual((out[0]["title"], out[0]["signatures"]), ("Ours", ["A"]))
        self.assertEqual([b.get("by_regressor") for b in out], [True, True])
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
            self.assertIsNone(bugzilla_apply._same_defect_candidates(42, "Foo::Bar", "Firefox",
                                                                     5))


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


class TestSignatureField(unittest.TestCase):
    def test_read_with_a_token_only_when_given(self):
        resp = mock.Mock()
        resp.json.return_value = {"bugs": [{"id": 7, "cf_crash_signature": "[@ A]"}]}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=resp) as get:
            self.assertEqual(bugzilla_apply._signature_field(7, token="tok"), "[@ A]")
            self.assertEqual(get.call_args.kwargs["headers"], {"X-Bugzilla-API-Key": "tok"})
            bugzilla_apply._signature_field(7)
            self.assertIsNone(get.call_args.kwargs["headers"])


class TestSameDefectBug(unittest.TestCase):
    _BUGS = [{"bug": 7, "summary": "s", "status": "NEW", "signatures": ["A"],
              "by_regressor": True}]

    _MIXED = [{"bug": 7, "summary": "s", "status": "NEW", "signatures": ["A"],
               "by_regressor": True},
              {"bug": 9, "summary": "r", "status": "NEW", "signatures": ["B"], "restricted": True,
               "by_regressor": True}]

    _BY_STACK = [{"bug": 7, "summary": "s", "status": "NEW", "signatures": ["A"],
                  "by_regressor": True},
                 {"bug": 30, "summary": "t", "status": "NEW", "signatures": ["C"],
                  "shared_frames": ["Foo::Layout"]}]

    def _run(self, answer, bugs=_BUGS, dossier=_DOSSIER, cfg=_SD_CFG, answers=None):
        async def run(*a, **k):
            return answers.pop(0) if answers is not None else answer
        with mock.patch.object(bugzilla_apply, "_same_defect_candidates",
                               return_value=bugs) as lookup, \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"ipc_fatal_error_msg": "Bad id"}) as reason, \
                mock.patch.object(same_defect, "run_same_defect", side_effect=run) as agent:
            out = bugzilla_apply._same_defect_bug("u-1", dict(_INFO, node="abc",
                                                              buildid=_BUILDID),
                                                  _STACK, dossier, "Foo::Bar", cfg)
        if agent.called:
            reason.assert_called_once_with("u-1")
        self.assertEqual(lookup.call_args.kwargs["buildid"], _BUILDID)
        self.assertEqual(lookup.call_args.kwargs["frames"], _STACK["frames"])
        self.assertEqual(lookup.call_args.args[0], dossier.get("candidate", {}).get("bug"))
        return out, agent

    def test_a_bug_listed_for_shared_frames(self):
        (bug, check), _ = self._run(same_defect.SameDefect(bug=30, confidence="medium"),
                                    bugs=self._BY_STACK)
        self.assertEqual(bug, 30)
        self.assertEqual((check["by_stack"], check["bugs"]), ([30], [7, 30]))
        (bug, check), _ = self._run(same_defect.SameDefect(bug=30, confidence="low"),
                                    bugs=self._BY_STACK)
        self.assertIsNone(bug)
        self.assertEqual(check["bug"], 30)

    def test_without_a_regressor_our_filings_are_still_compared(self):
        dossier = dict(_DOSSIER, candidate={})
        (bug, check), agent = self._run(same_defect.SameDefect(bug=30, confidence="high"),
                                        bugs=self._BY_STACK[1:], dossier=dossier)
        self.assertEqual(bug, 30)
        self.assertEqual((check["regressor"], check["by_stack"]), (None, [30]))
        self.assertIsNone(agent.call_args.args[1])

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

    def test_restricted_bugs_are_compared_on_their_own(self):
        (bug, check), agent = self._run(None, bugs=self._MIXED, answers=[
            same_defect.SameDefect(bug=None, confidence="high", reason="Public: no."),
            same_defect.SameDefect(bug=9, confidence="high", reason="Restricted: same.")])
        self.assertEqual([[b["bug"] for b in c.args[2]] for c in agent.call_args_list],
                         [[7], [9]])
        self.assertEqual(bug, 9)
        self.assertEqual((check["restricted"], check["bugs"], check["reason"]),
                         (True, [9], "Restricted: same."))

    def test_a_public_match_skips_the_restricted_comparison(self):
        (bug, check), agent = self._run(None, bugs=self._MIXED, answers=[
            same_defect.SameDefect(bug=7, confidence="high", reason="Same.")])
        self.assertEqual((bug, agent.call_count), (7, 1))
        self.assertEqual(check["bugs"], [7])
        self.assertNotIn("restricted", check)

    def test_no_restricted_match_leaves_no_record_of_them(self):
        (bug, check), agent = self._run(None, bugs=self._MIXED, answers=[
            same_defect.SameDefect(bug=None, confidence="high", reason="Public: no."),
            same_defect.SameDefect(bug=9, confidence="low", reason="Restricted: maybe.")])
        self.assertIsNone(bug)
        self.assertEqual(agent.call_count, 2)
        self.assertEqual((check["bugs"], check["reason"]), ([7], "Public: no."))
        self.assertNotIn("Restricted", repr(check))
        (bug, check), _ = self._run(None, bugs=self._MIXED[1:], answers=[None])
        self.assertEqual((bug, check), (None, None))

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
        self.assertEqual(self.puts, [(7, {"cf_crash_signature": "[@ A]\n[@ Foo::Bar]"})])
        self.assertEqual(self.filed, [("u-1", res)])
        self.assertEqual(res["same_defect"]["regressor"], 42)
        self.assertEqual((res["channel"], res["signature"]), ("nightly", "Foo::Bar"))

    def test_a_match_on_shared_frames_says_so_when_declined(self):
        bugzilla_apply._same_defect_bug.return_value = (30, dict(self._CHECK, bug=30,
                                                                 bugs=[30], by_stack=[30]))
        res = self._file(dossier=_DOSSIER, comment_on_existing="skip")
        self.assertEqual(res["skipped"], "bug 30 is the same defect; this channel does not "
                                         "write on existing bugs")

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

    def test_restricted_bugs_are_candidates_only_where_a_match_can_be_written(self):
        self._file(dossier=_DOSSIER)
        self.assertEqual(bugzilla_apply._same_defect_bug.call_args.kwargs["token"], "tok")
        self._file(dossier=_DOSSIER, comment_on_existing="skip")
        self.assertIsNone(bugzilla_apply._same_defect_bug.call_args.kwargs["token"])

    def test_a_restricted_match_is_read_with_the_token_and_withheld(self):
        check = dict(self._CHECK, bug=9, bugs=[9], restricted=True)
        bugzilla_apply._same_defect_bug.return_value = (9, check)
        res = self._file(dossier=_DOSSIER)
        self.assertEqual((res["filed"], res["bug"], res["findings_withheld"]), (True, 9, True))
        self.assertEqual(bugzilla_apply._signature_field.call_args.kwargs["token"], "tok")
        self.assertEqual(self.filed, [("u-1", res)])
        self.assertTrue(bugzilla_apply.disclosure.withheld_filing(res))
        bugzilla_apply._signature_field.return_value = "[@ A]\n[@ Foo::Bar]"
        self.assertTrue(self._file(dossier=_DOSSIER)["findings_withheld"])
        bugzilla_apply._signature_field.return_value = "[@ A]"
        bugzilla_apply._put_bug.side_effect = RuntimeError("503")
        res = self._file(dossier=_DOSSIER)
        self.assertEqual((res["filed"], res["findings_withheld"]), (False, True))
        self.assertTrue(self.errors[-1]["findings_withheld"])

    def _file_release(self):
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(
            comment_on_existing="comment_unless_dropped")
        return bugzilla_apply.autofile_bug("u-1", _RELEASE_INFO, {}, _DOSSIER, "lead", 70)

    def test_release_writes_on_a_match_unless_it_dropped_on_beta(self):
        with mock.patch.object(spikes, "upstream_drop", return_value=_upstream(False)) as drop:
            res = self._file_release()
        self.assertEqual((res["filed"], res["mode"], res["bug"]), (True, "same_defect", 7))
        self.assertIn("Foo::Bar", drop.call_args.args[0])
        self.assertEqual(drop.call_args.args[1:], ("Firefox", "release", "155.0.1"))
        self.assertEqual(bugzilla_apply._same_defect_bug.call_args.kwargs["token"], "tok")
        self.assertEqual(self.puts, [(7, {"cf_crash_signature": "[@ A]\n[@ Foo::Bar]"})])
        self.puts.clear()
        for upstream, why in ((_upstream(True), "it dropped on beta 156 (98 in 9599 crash "
                                                "reports, against 341 in 19750 on 155)"),
                              (None, "its trend on the upstream channel could not be read")):
            with mock.patch.object(spikes, "upstream_drop", return_value=upstream):
                res = self._file_release()
            self.assertFalse(res["filed"])
            self.assertEqual(res["skipped"], "bug 7 is the same defect, with the same regressor "
                                             "(bug 42); " + why)
            self.assertEqual((self.puts, self.created), ([], []))

    def test_release_asks_beta_only_after_a_match(self):
        bugzilla_apply._same_defect_bug.return_value = (None, dict(self._CHECK, bug=None))
        with mock.patch.object(spikes, "upstream_drop") as drop:
            res = self._file_release()
        self.assertEqual((res["filed"], res["mode"]), (True, "new_bug"))
        drop.assert_not_called()

    def test_a_public_match_is_read_anonymously(self):
        self._file(dossier=_DOSSIER)
        self.assertIsNone(bugzilla_apply._signature_field.call_args.kwargs["token"])

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


class TestOwnFilingsSharingFrames(unittest.TestCase):
    def _run(self, dossiers, spikes, stacks, frames, cfg=_SD_CFG):
        with mock.patch.object(bugzilla_apply.models.CrashStack, "top_frames",
                               return_value=stacks) as top:
            out = bugzilla_apply._own_filings_sharing_frames(frames, cfg, dossiers + spikes)
        if top.called:
            self.assertEqual(top.call_args.args[1], cfg["stack_depth"])
        return out

    def test_our_records_from_both_tables(self):
        d = [{"uuid": "u-1", "bug": 1}]
        e = [{"uuid": "u-2", "bug": 2, "escalation": 9}]
        for ours, theirs, out in ((d, e, d + e), (None, e, None), (d, None, None)):
            with mock.patch.object(bugzilla_apply.models.Dossier, "filing_records",
                                   return_value=ours), \
                    mock.patch.object(bugzilla_apply.models.SpikeEscalation, "filing_records",
                                      return_value=theirs):
                self.assertEqual(bugzilla_apply._own_filing_records(), out)

    def test_recent_filings_sharing_a_frame_best_first(self):
        now = bugzilla_apply.datetime.now(bugzilla_apply.timezone.utc)
        recent = (now - bugzilla_apply.timedelta(days=2)).isoformat()
        old = (now - bugzilla_apply.timedelta(days=40)).isoformat()
        dossiers = [{"uuid": "u-1", "bug": 1, "signature": "A", "mode": "new_bug", "at": recent},
                    {"uuid": "u-2", "bug": 2, "signature": "B", "mode": "new_bug", "at": old},
                    {"uuid": "u-3", "bug": 3, "signature": "C", "mode": "new_bug", "at": recent}]
        spikes = [{"uuid": "u-4", "bug": 4, "signature": "D", "mode": "spike_new_bug",
                   "at": recent, "escalation": 9}]
        stacks = {"u-1": [_f("Foo::Bar(int)", "foo.cpp"), _f("Foo::Layout", "foo.cpp")],
                  "u-2": [_f("Foo::Bar", "foo.cpp")],
                  "u-3": [_f("Other", "foo.cpp")],
                  "u-4": [_f("Foo::Layout", "foo.cpp"), _f("Foo::Bar", "bar.cpp")]}
        out = self._run(dossiers, spikes, stacks, _STACK["frames"])
        self.assertEqual([(f["bug"], names) for _score, names, f in out],
                         [(1, ["Foo::Bar", "Foo::Layout"]), (4, ["Foo::Layout"])])
        self.assertEqual(out[1][2]["escalation"], 9)

    def test_nothing_to_compare_and_failures(self):
        self.assertEqual(self._run([], [], {}, []), [])
        self.assertIsNone(self._run([], [], None, _STACK["frames"]))


class TestSpikeFiler(_FilerBase):
    """Spike filing with same-defect checks enabled."""

    _CHECK = {"regressor": None, "bugs": [30], "by_stack": [30], "bug": 30,
              "confidence": "high", "reason": "Same lambda.", "cost_usd": 0.05}

    def setUp(self):
        super().setUp()
        self.puts = []
        p = [
            mock.patch.object(config, "get_agent_same_defect", return_value=dict(_SD_CFG)),
            mock.patch.object(bugzilla_apply, "_match_same_defect",
                              return_value=(30, dict(self._CHECK))),
            mock.patch.object(bugzilla_apply, "_signature_field", return_value="[@ A]"),
            mock.patch.object(bugzilla_apply, "_put_bug",
                              side_effect=lambda b, c, t: self.puts.append((b, c))),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_the_signature_goes_onto_the_same_defect_bug(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["bug"], res["mode"]), (True, 30, "same_defect"))
        self.assertEqual(res["same_defect"]["reason"], "Same lambda.")
        self.assertEqual(self.puts, [(30, {"cf_crash_signature": "[@ A]\n[@ mozilla::Foo::Bar]"})])
        self.assertEqual((self.created, self.comments), ([], []))
        args, kwargs = bugzilla_apply._match_same_defect.call_args
        self.assertEqual(args[0], "mozilla::Foo::Bar")
        self.assertEqual(args[1], self.brief["stack_frames"]["frames"])
        self.assertEqual(args[2], {"bug": 22, "node": "2222222bbbbcccc"})
        self.assertEqual(args[3]["product"], "Firefox")
        self.assertEqual(kwargs["token"], "tok")
        crash = args[5]()
        self.assertEqual((crash["mechanism"], crash["crash_reason"]),
                         (self.findings.summary, "MOZ_CRASH(oops)"))

    def test_an_ungrounded_spike_has_no_regressor(self):
        se.file_spike_bug(_esc(), self.brief, self.findings, grounded=False)
        self.assertEqual(bugzilla_apply._match_same_defect.call_args.args[2], {})

    def test_no_match_files_a_new_bug_and_records_the_check(self):
        check = dict(self._CHECK, bug=None)
        bugzilla_apply._match_same_defect.return_value = (None, check)
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["mode"]), (True, "spike_new_bug"))
        self.assertEqual(res["same_defect_check"], check)
        self.assertEqual((len(self.created), self.puts), (1, []))

    def test_a_failed_write_files_nothing(self):
        bugzilla_apply._put_bug.side_effect = RuntimeError("503")
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertEqual(res["error"], "adding the signature to bug 30 failed")
        self.assertEqual(self.created, [])

    def test_a_restricted_match_is_withheld(self):
        bugzilla_apply._match_same_defect.return_value = (30, dict(self._CHECK, restricted=True))
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["findings_withheld"]), (True, True))
        self.assertEqual(bugzilla_apply._signature_field.call_args.kwargs["token"], "tok")

    def test_not_when_an_open_bug_is_the_venue(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[{"id": 1234, "creation_time": "2026-08-01T00:00:00Z",
                                              "product": "Core", "keywords": [],
                                              "regressed_by": [], "assigned_to": ""}]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=False)
        self.assertEqual(res["mode"], "spike_comment")
        bugzilla_apply._match_same_defect.assert_not_called()

    def test_not_for_a_withheld_crash(self):
        self.brief["raw_crash"] = {"x": 1}
        with mock.patch("crashclouseau.sensitive.memory_unsafe_signals",
                        return_value=["EXCEPTION_ACCESS_VIOLATION_WRITE"]):
            se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        bugzilla_apply._match_same_defect.assert_not_called()

    def test_disabled(self):
        config.get_agent_same_defect.return_value = dict(_SD_CFG, enabled=False)
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        bugzilla_apply._match_same_defect.assert_not_called()
        self.assertNotIn("same_defect_check", res)


_META = {"id": 1866944, "keywords": ["meta", "crash"], "product": "Core",
         "creation_time": "2023-11-20T00:00:00Z", "regressed_by": [], "assigned_to": ""}
_BUCKET_TITLE = "_dl_catch_error blocks BgIOThreadPool shutdown inside libsecret-1.so.0"
_WORK = {"title": _BUCKET_TITLE, "bucket": "_dl_catch_error | libsecret-1.so.0",
         "target": "nsThreadPool::ShutdownWithTimeout BgIOThreadPool", "name": "BgIOThreadPool",
         "files": ["security/manager/ssl/LibSecret.cpp", "security/manager/ssl/OSKeyStore.cpp"],
         "thread": {"index": 28, "name": "BgIOThr~Pool #3", "frames": [
             {"stackpos": 0, "function": "g_main_loop_run", "module": "libglib-2.0.so.0",
              "filename": "glib/gmain.c", "line": 4516},
             {"stackpos": 1, "function": "", "module": "libsecret-1.so.0", "filename": ""}]}}


class TestBucketPrompt(unittest.TestCase):
    def test_the_awaited_work_and_the_tracker_are_shown(self):
        crash = dict(same_defect.crash_from_dossier("Foo::Bar", _DOSSIER, _STACK["frames"]),
                     awaited={"name": "BgIOThr~Pool #3", "frames": _WORK["thread"]["frames"]})
        sib = {"bug": 2013019, "summary": "OS Key Store implementation can hang",
               "status": "NEW", "by_tracker": 1866944, "description": "libsecret hangs"}
        text = same_defect.user_prompt(crash, None, [sib])
        self.assertIn("The work this hang waits for, on thread `BgIOThr~Pool #3`:\n"
                      "  0 g_main_loop_run  (glib/gmain.c:4516)", text)
        self.assertIn("Listed because: blocks [meta] bug 1866944, which holds the crash's "
                      "signature", text)
        self.assertIn("they are the tracker's dependencies instead", same_defect._SYSTEM)


class TestBucketCandidates(unittest.TestCase):
    _DEPS = {10: 1866944, 11: 1866944, 12: 1866944, 13: 1866944, 14: 1866944, 15: 1866944,
             16: 1866944}

    def setUp(self):
        p = mock.patch.object(bugzilla_apply, "_tracker_dependencies",
                              return_value=dict(self._DEPS))
        p.start()
        self.addCleanup(p.stop)
        self.reads = []

    def _run(self, rows, extra=()):
        def read(ids, token=None, extra_fields=""):
            self.reads.append((list(ids), extra_fields))
            return [dict(r, description="comment 0 of {}".format(r["id"]))
                    for r in list(rows) + list(extra) if r["id"] in ids]
        with mock.patch.object(bugzilla_apply, "_crash_bug_rows", side_effect=read), \
                mock.patch.object(bugzilla_apply, "_bug_comments") as comments:
            out = bugzilla_apply._bucket_candidates([1866944], "Firefox", _BUILDID)
        comments.assert_not_called()
        return out

    def test_open_and_later_fixed_dependencies_newest_first(self):
        rows = [_row(10, sigs=(), created="2026-01-28T00:00:00Z"),
                _row(11, sigs=(), created="2026-10-12T00:00:00Z"),
                _row(12, resolution="FIXED", status="RESOLVED", resolved="2026-09-01T00:00:00Z"),
                _row(13, resolution="FIXED", status="RESOLVED", sigs=(),
                     created="2026-10-04T00:00:00Z", resolved="2026-10-19T00:00:00Z"),
                _row(14, keywords=("meta",)),
                _row(15, product="Thunderbird"),
                _row(16, resolution="WONTFIX", status="RESOLVED")]
        out = self._run(rows)
        self.assertEqual([b["bug"] for b in out], [11, 13, 10])
        first = out[0]
        self.assertEqual((first["by_tracker"], first["status"], first["signatures"]),
                         (1866944, "NEW", []))
        self.assertEqual(first["description"], "comment 0 of 11")
        self.assertNotIn("comments", first)
        self.assertEqual(out[1]["status"], "RESOLVED FIXED")
        self.assertEqual(self.reads[0], ([10, 11, 12, 13, 14, 15, 16], "description"))

    def test_large_trackers_are_read_in_batches(self):
        with mock.patch.object(bugzilla_apply, "_ROWS_PER_READ", 3):
            self._run([_row(10)])
        self.assertEqual([ids for ids, _ in self.reads], [[10, 11, 12], [13, 14, 15], [16]])

    def test_a_duplicate_is_replaced_by_its_target(self):
        rows = [_row(10, resolution="DUPLICATE", status="RESOLVED", dupe_of=99)]
        out = self._run(rows, extra=[_row(99, sigs=(), created="2026-10-04T00:00:00Z")])
        self.assertEqual([(b["bug"], b["by_tracker"], b["description"]) for b in out],
                         [(99, 1866944, "comment 0 of 99")])
        self.assertEqual(self.reads[-1], ([99], "description"))

    def test_failed_lookups_are_none(self):
        bugzilla_apply._tracker_dependencies.return_value = None
        self.assertIsNone(self._run([_row(10)]))
        bugzilla_apply._tracker_dependencies.return_value = dict(self._DEPS)
        with mock.patch.object(bugzilla_apply, "_crash_bug_rows", return_value=None):
            self.assertIsNone(bugzilla_apply._bucket_candidates([1866944], "Firefox", _BUILDID))


class TestTrackerDependencies(unittest.TestCase):
    def test_the_dependency_map(self):
        reply = mock.Mock()
        reply.json.return_value = {"bugs": [{"id": 1633342, "depends_on": [5, 6]},
                                            {"id": 1866944, "depends_on": [6, 7]}]}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=reply) as get:
            out = bugzilla_apply._tracker_dependencies([1866944, 1633342])
        self.assertEqual(out, {6: 1866944, 7: 1866944, 5: 1633342})
        self.assertEqual(get.call_args.kwargs["params"]["id"], "1866944,1633342")
        with mock.patch.object(bugzilla_apply.net, "get", side_effect=OSError("down")):
            self.assertIsNone(bugzilla_apply._tracker_dependencies([1866944]))

    def test_comment_0_is_an_extra_field(self):
        reply = mock.Mock()
        reply.json.return_value = {"bugs": []}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=reply) as get:
            bugzilla_apply._crash_bug_rows([7], extra_fields="description")
            self.assertTrue(get.call_args.kwargs["params"]["include_fields"].endswith(
                ",cf_last_resolved,description"))
            bugzilla_apply._crash_bug_rows([7])
            self.assertTrue(get.call_args.kwargs["params"]["include_fields"].endswith(
                ",cf_last_resolved"))

    def test_later_comments_of_several_bugs_in_one_read(self):
        reply = mock.Mock()
        reply.json.return_value = {"bugs": {
            "7": {"comments": [{"creator": "a@moz.example", "text": "c0"},
                               {"creator": "jstutte@mozilla.com", "text": "A different cohort."},
                               {"creator": "release-mgmt-account-bot@mozilla.tld",
                                "text": "topcrash"}]},
            "8": {"comments": [{"creator": "b@moz.example", "text": "c0"}]}}}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=reply) as get:
            out = bugzilla_apply._later_comments([7, 8])
        self.assertEqual(out, {7: [{"author": "jstutte@mozilla.com",
                                    "text": "A different cohort."}], 8: []})
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith("/7/comment"))
        self.assertEqual(get.call_args.kwargs["params"]["ids"], ["8"])
        self.assertNotIn("headers", get.call_args.kwargs)
        with mock.patch.object(bugzilla_apply.net, "get") as get:
            self.assertEqual(bugzilla_apply._later_comments([]), {})
            get.assert_not_called()
        with mock.patch.object(bugzilla_apply.net, "get", side_effect=OSError("down")):
            self.assertIsNone(bugzilla_apply._later_comments([7]))


class TestBucketRanking(unittest.TestCase):
    def test_the_rare_identifiers_rank_first(self):
        crash = {"title": _BUCKET_TITLE,
                 "mechanism": "An `OSKeyStore` job on `BgIOThreadPool` is stuck in "
                              "`LibSecret::StoreSecret`.",
                 "awaited": {"frames": _WORK["thread"]["frames"]}}
        terms = bugzilla_apply._bucket_terms(crash, _WORK)
        self.assertIn("oskeystore", terms)
        self.assertIn("libsecret", terms)
        # Exclude wait names and words outside code spans in the title.
        for common in ("bgiothreadpool", "shutdownwithtimeout", "blocks", "shutdown"):
            self.assertNotIn(common, terms)
        bugs = [{"bug": 1, "summary": "Suggest ingest blocks BgIOThreadPool shutdown",
                 "description": "viaduct send_sync"},
                {"bug": 2, "summary": "nsPrinterCUPS blocks BgIOThreadPool shutdown",
                 "description": "cupsCopyDestInfo"},
                {"bug": 3, "summary": "OS Key Store implementation can hang and cause shutdown "
                                      "hangs", "description": "On Linux the keyring prompt..."}]
        self.assertEqual([b["bug"] for b in bugzilla_apply._rank_bucket_candidates(terms, bugs)],
                         [3, 1, 2])

    def test_ties_keep_the_order(self):
        bugs = [{"bug": 1, "summary": "a"}, {"bug": 2, "summary": "b"}]
        self.assertEqual(bugzilla_apply._rank_bucket_candidates({"unrelated"}, bugs), bugs)

    def test_the_awaited_thread(self):
        self.assertEqual(bugzilla_apply._awaited_thread(_WORK)["name"], "BgIOThr~Pool #3")
        main = {"main": {"name": "Main Thread", "frames": [{"function": "F"}]}}
        self.assertEqual(bugzilla_apply._awaited_thread(main),
                         {"name": "Main Thread", "frames": [{"function": "F"}]})
        self.assertIsNone(bugzilla_apply._awaited_thread({"threads": 1}))
        self.assertIsNone(bugzilla_apply._awaited_thread(None))


class TestBucketBug(unittest.TestCase):
    _BUGS = [{"bug": 2080828, "summary": "nsPrinterCUPS blocks BgIOThreadPool shutdown",
              "status": "NEW", "signatures": [], "by_tracker": 1866944, "description": "cups"},
             {"bug": 2013019, "summary": "OS Key Store implementation can hang",
              "status": "NEW", "signatures": [], "by_tracker": 1866944,
              "description": "libsecret"}]

    _LATER = {2013019: [{"author": "jstutte@mozilla.com", "text": "The libsecret cohort."}]}

    def _run(self, answer, bugs=_BUGS, cfg=_SD_CFG, later=_LATER):
        async def run(*a, **k):
            return answer
        describe = mock.Mock(return_value={"signature": "Foo::Bar", "title": "",
                                           "mechanism": "An `OSKeyStore` job hangs.",
                                           "frames": []})
        with mock.patch.object(bugzilla_apply, "_bucket_candidates",
                               return_value=bugs) as lookup, \
                mock.patch.object(bugzilla_apply, "_later_comments",
                                  return_value=later) as comments, \
                mock.patch.object(same_defect, "run_same_defect", side_effect=run) as agent:
            out = bugzilla_apply._bucket_bug(
                "Foo::Bar", [], [_META], _WORK, _BUCKET_TITLE,
                dict(_INFO, node="abc", buildid=_BUILDID), cfg, describe)
        if bugs:
            self.assertEqual(lookup.call_args.args, ([1866944], "Firefox", _BUILDID))
        self.comments = comments
        return out, agent, describe

    def test_a_match_names_the_tracker(self):
        (bug, check), agent, describe = self._run(
            same_defect.SameDefect(bug=2013019, confidence="high", reason="Same libsecret."))
        self.assertEqual(bug, 2013019)
        self.assertEqual((check["tracker"], check["by_tracker"], check["regressor"]),
                         (1866944, [2013019, 2080828], None))
        self.assertNotIn("by_stack", check)
        self.assertEqual(bugzilla_apply._tracked_clause(bug, check),
                         "bug 2013019 already tracks this cause of [meta] bug 1866944")
        crash, regressor, listed = agent.call_args.args
        self.assertEqual((crash["title"], crash["awaited"]["name"]),
                         (_BUCKET_TITLE, "BgIOThr~Pool #3"))
        self.assertIsNone(regressor)
        self.assertEqual([(b["bug"], b["comments"]) for b in listed],
                         [(2013019, self._LATER[2013019]), (2080828, [])])
        describe.assert_called_once_with()

    def test_the_ranked_candidates_are_capped(self):
        (_, check), agent, _ = self._run(same_defect.SameDefect(bug=None, confidence="high"),
                                         cfg=dict(_SD_CFG, max_bugs=1))
        self.assertEqual([b["bug"] for b in agent.call_args.args[2]], [2013019])
        self.assertEqual(check["bugs"], [2013019])
        self.comments.assert_called_once_with([2013019])

    def test_unreadable_comments_of_the_selected_bugs(self):
        (bug, check), agent, _ = self._run(None, later=None)
        self.assertIsNone(bug)
        self.assertEqual(check["error"], bugzilla_apply._TRACKER_LOOKUP_FAILED)
        agent.assert_not_called()

    def test_below_the_floor_and_no_match(self):
        for answer in (same_defect.SameDefect(bug=2013019, confidence="low"),
                       same_defect.SameDefect(bug=None, confidence="high")):
            (bug, check), _, _ = self._run(answer)
            self.assertIsNone(bug)
            self.assertNotIn("tracker", check)

    def test_nothing_to_compare(self):
        (bug, check), agent, describe = self._run(None, bugs=[])
        self.assertEqual((bug, check), (None, None))
        agent.assert_not_called()
        describe.assert_not_called()

    def test_a_failed_lookup(self):
        (bug, check), agent, _ = self._run(None, bugs=None)
        self.assertIsNone(bug)
        self.assertEqual(check["error"], bugzilla_apply._TRACKER_LOOKUP_FAILED)
        agent.assert_not_called()


class TestTheFilerOnABucket(_Base):
    _CHECK = {"regressor": None, "bugs": [2013019], "by_tracker": [2013019], "bug": 2013019,
              "confidence": "high", "reason": "Same libsecret wait.", "tracker": 1866944}
    _DOSSIER = {"candidate": {"node": "n"}, "verdict": {"title": _BUCKET_TITLE},
                "corroborations": {"hang_awaited_work": _WORK}}

    def setUp(self):
        super().setUp()
        p = [
            mock.patch.object(bugzilla_apply.config, "get_agent_same_defect",
                              return_value=dict(_SD_CFG)),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                              return_value=[_bug(1866944, keywords=["meta"])]),
            mock.patch.object(bugzilla_apply, "_bucket_bug",
                              return_value=(2013019, dict(self._CHECK))),
            mock.patch.object(bugzilla_apply, "_same_defect_bug"),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_a_tracked_cause_is_not_filed_again(self):
        res = self._file(dossier=self._DOSSIER)
        self.assertFalse(res["filed"])
        self.assertEqual((res["bug"], res["meta_bugs"]), (2013019, [1866944]))
        self.assertEqual(res["skipped"],
                         "bug 2013019 already tracks this cause of [meta] bug 1866944")
        self.assertEqual(res["same_defect"]["reason"], "Same libsecret wait.")
        self.assertEqual((self.created, self.comments, self.puts, self.filed), ([], [], [], []))
        args = bugzilla_apply._bucket_bug.call_args.args
        self.assertEqual((args[2][0]["id"], args[3], args[4]), (1866944, _WORK, _BUCKET_TITLE))
        bugzilla_apply._same_defect_bug.assert_not_called()

    def test_an_unreadable_tracker_files_nothing(self):
        bugzilla_apply._bucket_bug.return_value = (
            None, {"regressor": None, "error": bugzilla_apply._TRACKER_LOOKUP_FAILED})
        res = self._file(dossier=self._DOSSIER)
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "could not read the bugs blocking [meta] bug 1866944; "
                                         "not risking a duplicate")
        self.assertEqual(self.created, [])

    def test_a_new_cause_files_its_bucket_bug_and_records_the_check(self):
        check = dict(self._CHECK, bug=None)
        del check["tracker"]
        bugzilla_apply._bucket_bug.return_value = (None, check)
        res = self._file(dossier=self._DOSSIER)
        self.assertEqual((res["filed"], res["mode"]), (True, "new_bug"))
        self.assertEqual(res["same_defect_check"], check)
        self.assertEqual(len(self.created), 1)
        bugzilla_apply._same_defect_bug.assert_not_called()

    def test_not_without_a_tracker_disabled_or_withheld(self):
        bugzilla_apply._same_defect_bug.return_value = (None, None)
        bugzilla_apply._open_bugs_for_signature.return_value = []
        self._file(dossier=self._DOSSIER)
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1866944, keywords=["meta"])]
        with mock.patch.object(bugzilla_apply.sensitive, "is_withheld", return_value=True):
            self._file(dossier=self._DOSSIER)
        bugzilla_apply.config.get_agent_same_defect.return_value = dict(_SD_CFG, enabled=False)
        self.assertEqual(self._file(dossier=self._DOSSIER)["mode"], "new_bug")
        bugzilla_apply._bucket_bug.assert_not_called()


class TestSpikeFilerOnABucket(_FilerBase):
    _CHECK = {"regressor": None, "bugs": [2013019], "by_tracker": [2013019], "bug": 2013019,
              "confidence": "high", "reason": "Same cause.", "tracker": 1866944}

    def setUp(self):
        super().setUp()
        p = [
            mock.patch.object(config, "get_agent_same_defect", return_value=dict(_SD_CFG)),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[_META]),
            mock.patch.object(bugzilla_apply, "_bucket_bug",
                              return_value=(2013019, dict(self._CHECK))),
            mock.patch.object(bugzilla_apply, "_match_same_defect"),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_a_tracked_cause_is_not_filed_again(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertEqual((res["bug"], res["venue_kind"]), (2013019, "tracked_bucket"))
        self.assertEqual(res["skipped"],
                         "bug 2013019 already tracks this cause of [meta] bug 1866944")
        self.assertEqual((self.created, self.comments), ([], []))
        args = bugzilla_apply._bucket_bug.call_args.args
        self.assertEqual((args[0], args[4]), ("mozilla::Foo::Bar",
                                              "The buffer allocator rewrite made a fresh "
                                              "content process OOM"))
        self.assertEqual(args[5]["product"], "Firefox")
        self.assertEqual(args[7]()["mechanism"], self.findings.summary)
        bugzilla_apply._match_same_defect.assert_not_called()

    def test_an_unreadable_tracker_is_retried(self):
        bugzilla_apply._bucket_bug.return_value = (
            None, {"regressor": None, "error": bugzilla_apply._TRACKER_LOOKUP_FAILED})
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["retry"]), (False, True))
        self.assertEqual(self.created, [])

    def test_a_new_cause_files_its_bucket_bug(self):
        bugzilla_apply._bucket_bug.return_value = (None, dict(self._CHECK, bug=None))
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["mode"]), (True, "spike_new_bug"))
        self.assertIsNone(res["same_defect_check"]["bug"])
        self.assertEqual(res["meta_bugs"], [1866944])

    def test_an_ungrounded_spike_goes_to_the_tracker_unchecked(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=False)
        self.assertEqual(res["venue_kind"], "meta")
        bugzilla_apply._bucket_bug.assert_not_called()


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

    def test_our_filings_and_their_top_frames(self):
        from datetime import date, datetime, timezone
        from crashclouseau import db, models
        models.create()
        build = models.Build(datetime(2026, 9, 24, 21, 39, 29, tzinfo=timezone.utc), "Firefox",
                             "nightly", "157.0a1", None)
        db.session.add(build)
        db.session.commit()
        sigid = models.Signature.get_id("Own::Filing")
        for name in ("of-1", "of-2"):
            db.session.add(models.UUID(name, sigid, name, build.id))
        db.session.commit()
        models.Dossier.upsert("of-1", payload={"dossier": {}}, status="done")
        models.Dossier.record_filed_bug("of-1", {"filed": True, "bug": 7, "mode": "new_bug",
                                                 "signature": "Own::Filing", "at": "2026-09-25"})
        models.Dossier.upsert("of-2", payload={"dossier": {}}, status="done")
        models.Dossier.record_filed_bug("of-2", {"filed": False, "bug": 8, "skipped": "x"})
        esc = models.SpikeEscalation("Own::Spike", "Firefox", "nightly", date(2026, 9, 24),
                                     uuid="of-2", payload={"filing": {
                                         "filed": True, "bug": 9, "mode": "spike_new_bug",
                                         "at": "2026-09-26"}})
        db.session.add(esc)
        db.session.commit()
        stack = {"frames": [dict(stackpos=i, original="", module="xul", filename="f.cpp",
                                 function="F{}".format(i), line=i, node="", internal=False,
                                 changesets={}) for i in range(4)]}
        # Match get_by_uuid() for a generated source stored without a filename.
        generated = "s3:gecko-generated-sources:abc/ipc/ipdl/PContentParent.cpp:"
        stack["frames"][1].update(filename="", original=generated)
        models.CrashStack.put_frames("of-1", stack, False)

        def cleanup():
            db.session.rollback()
            db.session.delete(esc)
            db.session.query(models.CrashStack).delete()
            db.session.delete(build)
            db.session.commit()
        self.addCleanup(cleanup)
        self.assertEqual(models.Dossier.filing_records(),
                         [{"uuid": "of-1", "bug": 7, "signature": "Own::Filing",
                           "mode": "new_bug", "at": "2026-09-25"}])
        self.assertEqual(models.SpikeEscalation.filing_records(),
                         [{"uuid": "of-2", "bug": 9, "signature": "Own::Spike",
                           "mode": "spike_new_bug", "at": "2026-09-26", "escalation": esc.id}])
        self.assertEqual(models.CrashStack.top_frames(["of-1", "of-2"], 2),
                         {"of-1": [{"stackpos": 0, "function": "F0", "filename": "f.cpp"},
                                   {"stackpos": 1, "function": "F1",
                                    "filename": "ipc/ipdl/PContentParent.cpp"}]})
        self.assertEqual(models.CrashStack.top_frames([], 2), {})


if __name__ == "__main__":
    unittest.main()
