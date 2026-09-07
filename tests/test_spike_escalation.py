# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A REAL spike files a bug, culprit or not; Claude Fable 5.1 gets one shot at the culprit.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_spike_escalation

Pure logic and wiring only: the sweep's gates, the investigator's options / prompt / handoff
parsing, the validation of what it says, the filer's branches, and the bug text. No network, no
SDK process, no Postgres. The predicate and the table are in tests/test_spikes.py.
"""
import asyncio
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import bugzilla_apply, config, models, report_bug, spike_report, utils  # noqa: E402
from crashclouseau.agent import spike_agent, spike_escalation as se  # noqa: E402
from crashclouseau.agent.spike_agent import SpikeFindings  # noqa: E402
from crashclouseau.agent.tools import crashstats  # noqa: E402


def _row(number, baseline, installs, outcome=utils.SELECTED, signature="mozilla::Foo::Bar",
         build_day="2026-09-03", picked="20260903093145", first_run=None, ever_selected=True):
    first_run = first_run or (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    return {"signature": signature, "product": "Firefox", "channel": "nightly",
            "build_day": build_day, "outcome": outcome, "number": number, "position": 5,
            "evaluable": True, "baseline": list(baseline),
            "bids": {picked: {"count": number, "installs": installs}}, "picked": picked,
            "run_date": first_run, "ever_selected": ever_selected, "first_run_date": first_run}


class TestConfig(unittest.TestCase):
    def test_shipped_knobs(self):
        cfg = config.get_agent_spike_escalation()
        self.assertTrue(cfg["enabled"])
        self.assertEqual((cfg["model"], cfg["effort"]), ("fable-5-1", "xhigh"))
        self.assertEqual(cfg["comment_on_existing"], "comment")
        self.assertGreater(cfg["job_timeout"], config.get_agent_job_timeout())

    def test_the_spend_switch_is_an_env_var(self):
        with mock.patch.dict(os.environ, {"SPIKE_ESCALATION_ENABLED": "0"}):
            self.assertFalse(config.get_agent_spike_escalation()["enabled"])

    def test_the_global_filing_switch_ignores_a_per_channel_hold(self):
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["channels"] = {**autofile["channels"], "beta": {"enabled": False}}
        agent["autofile"] = autofile
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}), \
                mock.patch.object(config, "get_agent", return_value=agent):
            self.assertTrue(config.autofile_globally_enabled())
            # ...while that channel's culprit filing is held.
            self.assertFalse(config.get_agent_autofile("beta")["enabled"])
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "0"}):
            self.assertFalse(config.autofile_globally_enabled(), "the kill switch still wins")

    def test_the_model_id_is_fable_5_1(self):
        from crashclouseau.agent import triage
        self.assertEqual(triage._model_id("fable-5-1"), "claude-fable-5-1")
        self.assertIn("claude-fable-5-1", config.get_llm()["pricing"])


class TestTheInvestigator(unittest.TestCase):
    _BRIEF = {"signature": "mozilla::Foo::Bar", "channel": "nightly", "product": "Firefox",
              "version": "157.0a1", "buildid": "20260903093145", "build_day": "2026-09-03",
              "uuid": "u-1", "pin_rev": "abc",
              "spike": {"kind": "build_day", "count": 32, "installs": 21, "baseline": [1, 0, 2],
                        "ratio": 16.0, "z": 7.3, "z_min": 3.62, "min_installs": 3},
              "spike_sentence": "32 reports from 21 distinct installations ...",
              "trend_sentence": "45 distinct installations in the last 7 days ...",
              "is_hang": True,
              "facts": ["Product: Firefox", "Report type: hang"],
              "stacks": [{"uuid": "u-1", "stack": "#0 Foo::Bar  foo.cpp:1", "share": "3 of 5"},
                         {"uuid": "u-2", "stack": "#0 Foo::Baz  foo.cpp:9", "share": "2 of 5",
                          "facts": ["OS: Windows 11"]}],
              "classic_runs": [{"uuid": "u-1", "status": "done", "verdict": "abstain",
                                "confidence": "low", "abstain_kind": "no_candidate_explains_it",
                                "abstain_reason": "nothing in the window", "candidate": None,
                                "mechanism": None, "second_opinion": None, "declined": None}],
              "candidates": {"onstack": [{"node": "1111111aaaa", "score": 7, "bug": 11,
                                          "desc": "Bug 11 - touch foo"}],
                             "window": [{"node": "2222222bbbb", "bug": 22, "desc": "Bug 22 - x",
                                         "pushdate": datetime(2026, 9, 3, tzinfo=timezone.utc),
                                         "pref_flip": True}],
                             "window_extent": "the 24 hours before this build"}}

    def test_options_are_fable_xhigh_with_the_builtin_toolset_off(self):
        opts = spike_agent.build_options(self._BRIEF, searchfox_client=object())
        self.assertEqual(opts.model, "claude-fable-5-1")
        self.assertEqual(opts.effort, "xhigh")
        self.assertEqual(opts.tools, [], "the CLI's Bash/Read/Write/WebFetch must not be registered")
        allowed = set(opts.allowed_tools)
        for t in ("mcp__crashstats__facets", "mcp__crashstats__report",
                  "mcp__socorro__crash_stats", "mcp__searchfox__define", "mcp__patch__diff",
                  "mcp__history__blame", "mcp__source__raw_file", "mcp__bugzilla__bug"):
            self.assertIn(t, allowed)
        for banned in ("Bash", "Read", "Grep", "Glob", "Task", "Agent", "WebFetch", "Write"):
            self.assertNotIn(banned, allowed)
        self.assertEqual(set(opts.mcp_servers),
                         {"searchfox", "patch", "history", "source", "bugzilla", "socorro",
                          "crashstats"})
        self.assertEqual(opts.env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"), "1")
        self.assertEqual(opts.fallback_model, "claude-opus-4-8")
        self.assertEqual(opts.max_budget_usd, 40.0)
        self.assertEqual(opts.permission_mode, "bypassPermissions")

    def test_the_system_prompt_names_every_handoff_field(self):
        for name in SpikeFindings.model_fields:
            self.assertIn('"{}"'.format(name), spike_agent._SYSTEM, name)
        for name in ("node", "bug", "confidence", "why"):
            self.assertIn('"{}"'.format(name), spike_agent._SYSTEM, name)
        self.assertIn("Never invent", spike_agent._SYSTEM)

    def test_the_brief_reaches_the_prompt(self):
        p = spike_agent._user_prompt(self._BRIEF)
        for text in ("32 reports from 21 distinct installations", "HANG / TIMEOUT",
                     "45 distinct installations in the last 7 days", "STACK 2", "u-2",
                     "OS: Windows 11", "no_candidate_explains_it", "1111111aaaa", "score=7",
                     "2222222bbbb", "[feature-flip]", "the 24 hours before this build",
                     "product::component"):
            self.assertIn(text, p)

    def test_a_pipeline_that_never_ran_is_said_so(self):
        p = spike_agent._user_prompt(dict(self._BRIEF, classic_runs=[]))
        self.assertIn("PRODUCED NO CONCLUSION", p)

    def test_parse_findings_is_lenient(self):
        text = ('reasoning...\n```json\n{"summary": "S", "assessment": "Regression", '
                '"product": "Core", "component": "DOM: Core & HTML", '
                '"culprit": {"node": "ABCDEF0123", "bug": "123", "confidence": "certain", '
                '"why": "w"}, "evidence": ["bare", {"claim": "c", "source": "s"}], '
                '"ruled_out": "one"}\n```')
        f = spike_agent.parse_findings(text)
        self.assertEqual(f.assessment, "regression")
        self.assertEqual((f.culprit.node, f.culprit.bug, f.culprit.confidence),
                         ("abcdef0123", 123, "low"))
        self.assertEqual(len(f.evidence), 2)
        self.assertEqual(f.ruled_out, ["one"])
        self.assertIsNone(spike_agent.parse_findings("no block at all"))
        self.assertIsNone(spike_agent.parse_findings(None))
        # A culprit without a node is no culprit.
        f = spike_agent.parse_findings('```json\n{"summary": "S", "culprit": {"bug": 1}}\n```')
        self.assertIsNone(f.culprit)

    def test_a_run_with_no_tool_call_grounded_nothing(self):
        run = spike_agent.SpikeRun()
        self.assertFalse(run.grounded)
        run.tool_calls = 1
        self.assertTrue(run.grounded)


class TestValidation(unittest.TestCase):
    def _brief(self):
        return {"channel": "nightly", "buildid": "20260903093145",
                "candidate_nodes": {"2222222bbbbcccc": {"node": "2222222bbbbcccc", "bug": 22}}}

    def test_a_culprit_from_the_window_is_kept_and_completed(self):
        f = SpikeFindings(summary="S", culprit={"node": "2222222bbbb", "confidence": "high"},
                          evidence=[{"claim": "c", "source": "s"}, {"claim": "no source"}])
        brief = self._brief()
        out, dropped = se.validate_findings(f, brief)
        self.assertEqual((out.culprit.node, out.culprit.bug), ("2222222bbbbcccc", 22))
        self.assertTrue(brief["culprit_in_window"])
        self.assertEqual([e.claim for e in out.evidence], ["c"])
        self.assertEqual(len(dropped), 1)
        self.assertIn("without a source", dropped[0])

    def test_an_unknown_hash_hg_cannot_resolve_is_dropped(self):
        f = SpikeFindings(summary="S", culprit={"node": "deadbeefcafe", "confidence": "high"})
        with mock.patch.object(se, "_pushdate", return_value=None):
            out, dropped = se.validate_findings(f, self._brief())
        self.assertIsNone(out.culprit)
        self.assertIn("not a changeset hg knows", dropped[0])

    def test_a_changeset_landed_after_the_build_is_dropped(self):
        f = SpikeFindings(summary="S", culprit={"node": "deadbeefcafe", "confidence": "high"})
        late = datetime(2026, 9, 4, tzinfo=timezone.utc)
        with mock.patch.object(se, "_pushdate", return_value=late):
            out, dropped = se.validate_findings(f, self._brief())
        self.assertIsNone(out.culprit)
        self.assertIn("landed after", dropped[0])

    def test_an_older_resolvable_changeset_is_kept_but_not_in_window(self):
        f = SpikeFindings(summary="S", culprit={"node": "deadbeefcafe", "confidence": "medium"})
        early = datetime(2026, 8, 20, tzinfo=timezone.utc)
        brief = self._brief()
        with mock.patch.object(se, "_pushdate", return_value=early):
            out, dropped = se.validate_findings(f, brief)
        self.assertIsNotNone(out.culprit)
        self.assertFalse(brief["culprit_in_window"])
        self.assertEqual(dropped, [])

    def test_a_non_hash_is_dropped(self):
        f = SpikeFindings(summary="S", culprit={"node": "bug 12345", "confidence": "high"})
        out, dropped = se.validate_findings(f, self._brief())
        self.assertIsNone(out.culprit)

    def test_nothing_to_validate(self):
        self.assertEqual(se.validate_findings(None, self._brief()), (None, []))


class TestComponent(unittest.TestCase):
    def test_the_investigators_pair_is_checked_against_bugzilla(self):
        f = SpikeFindings(summary="S", product="Core", component="dom: core & html")
        with mock.patch.object(se, "_components_of", return_value={"DOM: Core & HTML", "General"}):
            self.assertEqual(se.resolve_component(f, "sig", "Firefox"),
                             ("Core", "DOM: Core & HTML", "investigator"))

    def test_an_unknown_component_falls_back_to_the_signatures_bugs_then_general(self):
        f = SpikeFindings(summary="S", product="Core", component="Made Up")
        with mock.patch.object(se, "_components_of", return_value={"General"}), \
                mock.patch.object(se, "_component_from_signature_bugs",
                                  return_value=("Toolkit", "Storage")):
            self.assertEqual(se.resolve_component(f, "sig", "Firefox"),
                             ("Toolkit", "Storage", "the signature's existing bugs"))
        with mock.patch.object(se, "_components_of", return_value={"General"}), \
                mock.patch.object(se, "_component_from_signature_bugs",
                                  return_value=(None, None)):
            self.assertEqual(se.resolve_component(f, "sig", "Firefox"),
                             ("Core", "General", "fallback"))

    def test_another_applications_product_is_never_used(self):
        f = SpikeFindings(summary="S", product="MailNews Core", component="Networking")
        with mock.patch.object(se, "_components_of", return_value={"Networking"}), \
                mock.patch.object(se, "_component_from_signature_bugs",
                                  return_value=(None, None)):
            self.assertEqual(se.resolve_component(f, "sig", "Firefox")[:2], ("Core", "General"))

    def test_bugzilla_unreachable_takes_the_investigators_word(self):
        f = SpikeFindings(summary="S", product="Core", component="Graphics")
        with mock.patch.object(se, "_components_of", return_value=None):
            self.assertEqual(se.resolve_component(f, "sig", "Firefox")[:2], ("Core", "Graphics"))

    def test_the_signature_bugs_vote(self):
        bugs = [
            {"id": 1, "product": "Core", "component": "Storage", "resolution": "FIXED",
             "cf_crash_signature": "[@ sig]"},
            {"id": 2, "product": "Core", "component": "Networking", "resolution": "",
             "cf_crash_signature": "[@ sig]"},
            {"id": 3, "product": "Thunderbird", "component": "General", "resolution": "",
             "cf_crash_signature": "[@ sig]"},
            {"id": 4, "product": "Core", "component": "Other", "resolution": "",
             "cf_crash_signature": "[@ sig | more]"},
        ]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"bugs": bugs}
        resp.raise_for_status.return_value = None
        with mock.patch.object(se.net, "get", return_value=resp):
            self.assertEqual(se._component_from_signature_bugs("sig", "Firefox"),
                             ("Core", "Networking"), "open bugs outrank fixed, other apps are out")


class TestVenue(unittest.TestCase):
    def test_a_bug_filed_for_the_spike_wins_over_the_oldest(self):
        old = {"id": 1, "creation_time": "2024-01-01T00:00:00Z"}
        new = {"id": 9, "creation_time": "2026-09-03T08:00:00Z"}
        venue, for_spike = se._pick_venue([old, new], date(2026, 9, 3))
        self.assertEqual((venue["id"], for_spike), (9, True))
        venue, for_spike = se._pick_venue([old], date(2026, 9, 3))
        self.assertEqual((venue["id"], for_spike), (1, False))
        self.assertIsNone(se._pick_venue([], date(2026, 9, 3)))


def _esc(**over):
    base = dict(id=7, signature="mozilla::Foo::Bar", product="Firefox", channel="nightly",
                build_day=date(2026, 9, 3), buildid="20260903093145", uuid="u-1",
                payload={"spike": {"kind": "build_day"}}, status="pending", attempts=0)
    base.update(over)
    return SimpleNamespace(**base)


class _FilerBase(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.comments = []
        self.brief = {"signature": "mozilla::Foo::Bar", "channel": "nightly",
                      "product": "Firefox", "version": "157.0a1", "buildid": "20260903093145",
                      "build_day": "2026-09-03", "uuid": "u-1", "raw_crash": {},
                      "stack_frames": {"frames": [{"stackpos": 0, "function": "Foo::Bar",
                                                   "filename": "foo.cpp", "line": 1,
                                                   "module": "xul.dll"}]},
                      "spike": {"kind": "build_day", "count": 32, "installs": 21,
                                "baseline": [1, 0, 2], "ratio": 16.0, "z": 7.3, "z_min": 3.62},
                      "spike_sentence": "32 reports from 21 distinct installations on the nightly build",
                      "stacks": [{"uuid": "u-1"}], "candidate_nodes": {}}
        self.findings = SpikeFindings(
            summary="The buffer allocator rewrite made a fresh content process OOM.",
            product="Core", component="JavaScript: GC",
            culprit={"node": "2222222bbbbcccc", "bug": 22, "confidence": "high", "why": "w"},
            evidence=[{"claim": "c", "source": "s"}])
        patches = [
            mock.patch.object(config, "autofile_globally_enabled", return_value=True),
            mock.patch.object(config, "get_bugzilla_token", return_value="tok"),
            mock.patch.object(models.SpikeEscalation, "count_since", return_value=0),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[]),
            mock.patch.object(bugzilla_apply, "_post_comment",
                              side_effect=lambda b, t, p, tok: self.comments.append((b, t)) or 1),
            mock.patch.object(bugzilla_apply, "_create_bug_keeping_the_bug",
                              side_effect=lambda p, tok: (self.created.append(p) or (2070000, False))),
            mock.patch.object(bugzilla_apply, "_link_blockers", side_effect=lambda b, w, t: list(w)),
            mock.patch.object(bugzilla_apply, "_link_regressed_by",
                              side_effect=lambda b, r, t: list(r)),
            mock.patch.object(bugzilla_apply, "_nominate_tracking", return_value=True),
            mock.patch.object(bugzilla_apply, "_set_needinfo", return_value=None),
            mock.patch.object(report_bug, "fetch_crash_reason",
                              return_value={"moz_crash_reason": "MOZ_CRASH(oops)"}),
            mock.patch.object(report_bug, "security_group", return_value="core-security"),
            mock.patch.object(se, "resolve_component",
                              return_value=("Core", "JavaScript: GC", "investigator")),
            # Nothing below the public open bugs unless a test says so: no earlier filing of
            # ours, no bug fixed after the build.
            mock.patch.object(models.Dossier, "already_filed_for_signature", return_value=None),
            mock.patch.object(models.SpikeEscalation, "prior_bug_for", return_value=None),
            mock.patch.object(bugzilla_apply, "_fixed_bugs_about", return_value=[]),
            mock.patch.object(se, "_bug_state", return_value=None),
            mock.patch.object(se, "_needinfo_person_for",
                              return_value={"nick": "dev", "account": "dev@moz.example",
                                            "name": "Dev", "email": "dev@moz.example"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestFiling(_FilerBase):
    def test_a_new_bug_carries_the_volume_the_analysis_and_the_marks(self):
        self.brief["culprit_in_window"] = True
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (2070000, "spike_new_bug"))
        self.assertEqual((res["product"], res["component"]), ("Core", "JavaScript: GC"))
        self.assertEqual(res["regressed_by"], [22])
        self.assertEqual(res["needinfo"], "dev@moz.example")
        payload = self.created[0]
        self.assertEqual(payload["summary"], "Crash in [@ mozilla::Foo::Bar]")
        self.assertEqual(payload["product"], "Core")
        self.assertEqual(payload["cf_crash_signature"], "[@ mozilla::Foo::Bar]")
        self.assertEqual(payload["version"], "Trunk")
        self.assertIn("regression", payload["keywords"])
        self.assertEqual(payload["flags"][0]["requestee"], "dev@moz.example")
        self.assertNotIn("groups", payload)
        body = payload["description"]
        self.assertIn("Filed because this signature's crash volume spiked", body)
        self.assertIn("32 reports from 21 distinct installations", body)
        self.assertIn("buffer allocator rewrite", body)
        self.assertIn("Suspected regressor (high confidence)", body)
        self.assertIn("2222222bbbbcccc", body)
        self.assertIn(":dev, can you have a look please?", body)
        self.assertIn("Top 1 frames", body)
        self.assertIn("MOZ_CRASH(oops)", body)

    def test_a_culprit_outside_the_window_is_a_starting_point_without_regressed_by(self):
        self.brief["culprit_in_window"] = False
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["filed"])
        self.assertNotIn("regressed_by", res)
        body = self.created[0]["description"]
        self.assertIn("NOT an established cause", body)

    def test_no_findings_still_files_the_volume(self):
        res = se.file_spike_bug(_esc(), self.brief, None, grounded=False)
        self.assertTrue(res["filed"])
        body = self.created[0]["description"]
        self.assertIn("The volume above is the finding", body)
        self.assertNotIn("Suspected regressor", body)
        self.assertEqual(self.created[0]["keywords"], ["crash"])
        self.assertNotIn("flags", self.created[0], "no culprit, nobody to ask")

    def test_an_ungrounded_run_publishes_no_analysis(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=False)
        self.assertTrue(res["filed"])
        body = self.created[0]["description"]
        self.assertIn("consulted no source", body)
        self.assertNotIn("buffer allocator rewrite", body)
        self.assertEqual(self.created[0]["keywords"], ["crash"])

    def test_an_open_bug_on_the_signature_gets_a_comment(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                {"id": 55, "creation_time": "2024-01-01T00:00:00Z", "product": "Core",
                 "keywords": [], "regressed_by": [1]}]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["bug"], res["mode"]), (True, 55, "spike_comment"))
        self.assertFalse(res["venue_for_spike"])
        self.assertEqual(self.created, [])
        self.assertIn("crash volume spiked", self.comments[0][1])

    def test_a_bug_filed_for_this_spike_that_names_its_regressor_gets_nothing(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                {"id": 66, "creation_time": "2026-09-03T06:00:00Z", "product": "Core",
                 "keywords": [], "regressed_by": [22]}]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertIn("already names its regressor", res["skipped"])
        self.assertEqual((self.created, self.comments), ([], []))

    def test_a_thunderbird_bug_or_a_meta_is_not_a_venue(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                {"id": 1, "creation_time": "2024-01-01T00:00:00Z", "product": "Thunderbird",
                 "keywords": [], "regressed_by": []},
                {"id": 2, "creation_time": "2024-01-01T00:00:00Z", "product": "Core",
                 "keywords": ["meta"], "regressed_by": []}]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        body = self.created[0]["description"]
        self.assertIn("bug 1", body)
        self.assertIn("bug 2", body)

    def test_skip_and_file_new_modes(self):
        existing = [{"id": 55, "creation_time": "2024-01-01T00:00:00Z", "product": "Core",
                     "keywords": [], "regressed_by": []}]
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=existing), \
                mock.patch.object(config, "get_agent_spike_escalation",
                                  return_value=dict(config.get_agent_spike_escalation(),
                                                    comment_on_existing="skip")):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["skipped"], "open bug 55 exists")
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=existing), \
                mock.patch.object(config, "get_agent_spike_escalation",
                                  return_value=dict(config.get_agent_spike_escalation(),
                                                    comment_on_existing="file_new")):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["related_bugs"]), ("spike_new_bug", [55]))
        self.assertIn("filed as a new bug", self.created[0]["description"])

    def test_a_per_channel_culprit_hold_does_not_hold_a_spike(self):
        # A channel whose ordinary filing is held (`channels.<ch>.enabled: false`, as beta was
        # until 2026-09-07) still files a real spike: the hold is about culprit filings.
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["channels"] = {**autofile["channels"], "beta": {"enabled": False}}
        agent["autofile"] = autofile
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}), \
                mock.patch.object(config, "get_agent", return_value=agent):
            self.assertFalse(config.get_agent_autofile("beta")["enabled"])
            res = se.file_spike_bug(_esc(channel="beta"), dict(self.brief, channel="beta"),
                                    self.findings, grounded=True)
        self.assertTrue(res["filed"])
        self.assertEqual(self.created[0]["version"], "unspecified")

    def test_the_global_kill_switch_wins(self):
        with mock.patch.object(config, "autofile_globally_enabled", return_value=False):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["skipped"], "autofile disabled")
        self.assertEqual(self.created, [])

    def test_the_daily_cap_and_a_failed_lookup_are_retried_later(self):
        with mock.patch.object(models.SpikeEscalation, "count_since", return_value=3):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["retry"])
        self.assertIn("daily cap", res["skipped"])
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=None):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["retry"])

    def test_a_memory_safety_crash_is_restricted_or_not_filed(self):
        self.brief["raw_crash"] = {"json_dump": {"crash_info": {"address": "0xe5e5e5e5e5e5e5e5"}}}
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["filed"])
        self.assertEqual(self.created[0]["groups"], ["core-security"])
        self.assertEqual(res["security_groups"], ["core-security"])
        with mock.patch.object(report_bug, "security_group", return_value=None):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertIn("no security group", res["skipped"])

    def test_a_memory_safety_crash_declines_a_public_venue(self):
        self.brief["raw_crash"] = {"json_dump": {"crash_info": {"address": "0xe5e5e5e5e5e5e5e5"}}}
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                {"id": 55, "creation_time": "2024-01-01T00:00:00Z", "product": "Core",
                 "keywords": [], "regressed_by": []}]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["public_venue_declined"]), ("spike_new_bug", 55))
        self.assertEqual(self.comments, [])
        self.assertIn("Probably a duplicate of bug 55", self.created[0]["description"])

    def test_an_unsymbolicated_signature_is_not_filed(self):
        res = se.file_spike_bug(_esc(signature="@0xdeadbeef"), dict(self.brief, signature="@0xdeadbeef"),
                                self.findings, grounded=True)
        self.assertIn("unsymbolicated", res["skipped"])

    def test_a_bugzilla_rejection_is_recorded_not_raised(self):
        with mock.patch.object(bugzilla_apply, "_create_bug_keeping_the_bug",
                               side_effect=RuntimeError("boom")):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertNotIn("retry", res, "the write may have landed; never re-post blindly")

    def test_release_nominates_tracking_and_marks_an_appearance(self):
        brief = dict(self.brief, channel="release", version="155.0.1")
        res = se.file_spike_bug(_esc(channel="release"), brief, self.findings, grounded=True)
        self.assertEqual(res["tracking_nominated"], "cf_tracking_firefox155")
        self.assertEqual(self.created[0]["summary"], "Crash in [@ mozilla::Foo::Bar]",
                         "an old signature that got loud (baseline 1, 0, 2) is not new in release")
        # ...0, 0, 0 -> 50 with no earlier report on release IS new in release.
        brief["spike"] = dict(brief["spike"], count=50, installs=50, baseline=[0, 0, 0], ratio=None)
        brief["first_seen_channel"] = "20260903093145"
        res = se.file_spike_bug(_esc(channel="release"), brief, self.findings, grounded=True)
        self.assertTrue(res["filed"])
        self.assertEqual(self.created[1]["summary"], "[new in release] Crash in [@ mozilla::Foo::Bar]")


class TestTheVenueBelowThePublicBugs(_FilerBase):
    """Clouseau filed a bug on nightly for this signature; later it really spikes on beta."""

    _RESOLVED_AFTER = "2026-09-05T10:00:00Z"      # the spiking build is 20260903093145
    _RESOLVED_BEFORE = "2026-09-01T10:00:00Z"

    def _beta(self):
        return _esc(channel="beta"), dict(self.brief, channel="beta", siblings=["mozilla::Foo::Bar"])

    def test_our_open_nightly_bug_is_the_venue_for_the_beta_spike(self):
        esc, brief = self._beta()
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                {"id": 70, "creation_time": "2026-08-30T00:00:00Z", "product": "Core",
                 "keywords": [], "regressed_by": []}]):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["bug"], res["venue_kind"]), ("spike_comment", 70, "open"))
        self.assertEqual(self.created, [])

    def test_our_restricted_open_bug_still_gets_the_spike(self):
        # Public lookup sees nothing (a human restricted our bug); the database remembers it.
        esc, brief = self._beta()
        with mock.patch.object(models.Dossier, "already_filed_for_signature",
                               return_value={"uuid": "u-0", "bug": 71}), \
                mock.patch.object(se, "_bug_state", return_value={
                    "id": 71, "status": "NEW", "resolution": "", "resolved": None,
                    "assigned_to": "nobody@mozilla.org"}):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["bug"], res["venue_kind"]),
                         ("spike_comment", 71, "own_restricted"))
        self.assertEqual(self.created, [])

    def test_our_fixed_bug_gets_the_spike_when_the_fix_postdates_the_build(self):
        esc, brief = self._beta()
        with mock.patch.object(models.Dossier, "already_filed_for_signature",
                               return_value={"uuid": "u-0", "bug": 72}), \
                mock.patch.object(se, "_bug_state", return_value={
                    "id": 72, "status": "RESOLVED", "resolution": "FIXED",
                    "resolved": datetime(2026, 9, 5, 10, tzinfo=timezone.utc),
                    "assigned_to": "fixer@moz.example"}), \
                mock.patch.object(report_bug, "_person_for_account",
                                  return_value={"nick": "fixer", "account": "fixer@moz.example"}), \
                mock.patch.object(se, "_needinfo_person_for", return_value={}):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["bug"], res["venue_kind"]), ("spike_comment", 72, "fixed"))
        text = self.comments[0][1]
        self.assertTrue(text.startswith("**Bug 72 is RESOLVED FIXED"))
        self.assertIn("after build 20260903093145 was produced", text)
        self.assertIn("beta builds that do not carry the fix", text)
        self.assertIn(":fixer, can you have a look please?", text, "the fixer is the one to ask")
        self.assertEqual(res["needinfo"], "fixer@moz.example")
        self.assertEqual(self.created, [])

    def test_a_fix_already_in_the_build_means_a_new_bug(self):
        esc, brief = self._beta()
        with mock.patch.object(models.Dossier, "already_filed_for_signature",
                               return_value={"uuid": "u-0", "bug": 73}), \
                mock.patch.object(se, "_bug_state", return_value={
                    "id": 73, "status": "RESOLVED", "resolution": "FIXED",
                    "resolved": datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
                    "assigned_to": "fixer@moz.example"}):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertEqual(self.comments, [])

    def test_a_bug_closed_invalid_or_duplicate_is_not_a_venue(self):
        esc, brief = self._beta()
        for resolution in ("INVALID", "DUPLICATE", "WORKSFORME"):
            self.created.clear()
            with mock.patch.object(models.Dossier, "already_filed_for_signature",
                                   return_value={"uuid": "u-0", "bug": 74}), \
                    mock.patch.object(se, "_bug_state", return_value={
                        "id": 74, "status": "RESOLVED", "resolution": resolution,
                        "resolved": datetime(2026, 9, 5, tzinfo=timezone.utc), "assigned_to": ""}):
                res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
            self.assertEqual(res["mode"], "spike_new_bug", resolution)

    def test_anybodys_bug_fixed_after_the_build_gets_the_spike(self):
        esc, brief = self._beta()
        row = {"id": 88, "assigned_to": "owner@moz.example", "product": "Core"}
        with mock.patch.object(bugzilla_apply, "_fixed_bugs_about", return_value=[
                (row, datetime(2026, 9, 5, tzinfo=timezone.utc))]), \
                mock.patch.object(report_bug, "_person_for_account", return_value={}):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["bug"], res["venue_kind"]), ("spike_comment", 88, "fixed"))
        # Our own prior filing outranks anybody's fixed bug.
        with mock.patch.object(bugzilla_apply, "_fixed_bugs_about", return_value=[
                (row, datetime(2026, 9, 5, tzinfo=timezone.utc))]), \
                mock.patch.object(models.SpikeEscalation, "prior_bug_for", return_value=75), \
                mock.patch.object(se, "_bug_state", return_value={
                    "id": 75, "status": "REOPENED", "resolution": "", "resolved": None,
                    "assigned_to": ""}):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual((res["bug"], res["venue_kind"]), (75, "own_restricted"))

    def test_a_memory_safety_crash_never_comments_on_a_public_fixed_bug(self):
        esc, brief = self._beta()
        brief["raw_crash"] = {"json_dump": {"crash_info": {"address": "0xe5e5e5e5e5e5e5e5"}}}
        with mock.patch.object(bugzilla_apply, "_fixed_bugs_about", return_value=[
                ({"id": 88, "assigned_to": ""}, datetime(2026, 9, 5, tzinfo=timezone.utc))]):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["public_venue_declined"]), ("spike_new_bug", 88))
        self.assertEqual(self.created[0]["groups"], ["core-security"])
        self.assertIn("Probably a duplicate of bug 88", self.created[0]["description"])

    def test_skip_mode_writes_on_no_existing_bug_at_all(self):
        esc, brief = self._beta()
        with mock.patch.object(bugzilla_apply, "_fixed_bugs_about", return_value=[
                ({"id": 88, "assigned_to": ""}, datetime(2026, 9, 5, tzinfo=timezone.utc))]), \
                mock.patch.object(config, "get_agent_spike_escalation",
                                  return_value=dict(config.get_agent_spike_escalation(),
                                                    comment_on_existing="skip")):
            res = se.file_spike_bug(esc, brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertEqual(self.comments, [])


class _FakeEscalation:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.statuses = []

    def set_status(self, status, error=None, commit=True):
        self.status = status
        self.statuses.append(status)


class TestTheSweep(unittest.TestCase):
    def setUp(self):
        self.cfg = config.get_agent_spike_escalation()
        self.created = []
        self.enqueued = []

        def create(signature, product, channel, build_day, buildid=None, uuid=None,
                   kind="build_day", payload=None, commit=True):
            row = _FakeEscalation(id=len(self.created) + 1, signature=signature, product=product,
                                  channel=channel, build_day=build_day, buildid=buildid,
                                  uuid=uuid, kind=kind, payload=payload or {}, status="pending",
                                  attempts=0)
            self.created.append(row)
            return row

        patches = [
            mock.patch.object(models.SpikeEscalation, "for_pair", return_value=None),
            mock.patch.object(models.SpikeEscalation, "latest_for_signature", return_value=None),
            mock.patch.object(models.SpikeEscalation, "count_since", return_value=0),
            mock.patch.object(models.SpikeEscalation, "create", side_effect=create),
            mock.patch.object(se, "_enqueue", side_effect=lambda i, c: self.enqueued.append(i)),
            mock.patch.object(se, "classic_runs", return_value=[
                {"uuid": "u-1", "status": "done", "verdict": "abstain", "filed_bug": None}]),
            mock.patch.object(se, "representative_uuid", return_value="u-1"),
            mock.patch.object(se, "_trend", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _sweep(self, rows, room=2):
        with mock.patch.object(models.Selection, "escalation_candidates", return_value=rows):
            return se._sweep_channel("Firefox", "nightly", self.cfg, room)

    def test_a_real_spike_the_pipeline_did_not_file_is_escalated(self):
        n = self._sweep([_row(32, [1, 0, 2], 21)])
        self.assertEqual(n, 1)
        self.assertEqual(self.enqueued, [1])
        row = self.created[0]
        self.assertEqual((row.signature, row.buildid, row.uuid, row.kind),
                         ("mozilla::Foo::Bar", "20260903093145", "u-1", "build_day"))
        self.assertEqual(row.payload["spike"]["count"], 32)
        self.assertIn("32 reports from 21", row.payload["spike_sentence"])
        self.assertEqual(row.payload["classic_runs"], 1)

    def test_zero_to_one_is_never_escalated(self):
        self.assertEqual(self._sweep([_row(1, [0, 0, 0], 1), _row(4, [0, 0, 0], 4)]), 0)
        self.assertEqual(self.created, [])

    def test_the_grace_period_lets_the_ordinary_runs_settle(self):
        fresh = datetime.now(timezone.utc).isoformat()
        self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21, first_run=fresh)]), 0)

    def test_a_pending_ordinary_run_defers(self):
        with mock.patch.object(se, "classic_runs", return_value=[
                {"uuid": "u-1", "status": "running", "filed_bug": None}]):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)
        self.assertEqual(self.created, [])

    def test_a_spike_the_ordinary_path_filed_is_recorded_and_left_alone(self):
        with mock.patch.object(se, "classic_runs", return_value=[
                {"uuid": "u-1", "status": "done", "filed_bug": {"filed": True, "bug": 2069800}}]):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)
        self.assertEqual(self.enqueued, [])
        row = self.created[0]
        self.assertEqual(row.status, "done")
        self.assertIn("2069800", row.payload["skipped"])

    def test_one_escalation_per_episode(self):
        with mock.patch.object(models.SpikeEscalation, "latest_for_signature",
                               return_value=_FakeEscalation(id=3)):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)
        with mock.patch.object(models.SpikeEscalation, "for_pair",
                               return_value=_FakeEscalation(id=3, status="done", attempts=1)):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)

    def test_a_failed_escalation_is_retried_once(self):
        with mock.patch.object(models.SpikeEscalation, "for_pair",
                               return_value=_FakeEscalation(id=3, status="error", attempts=1)):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 1)
        self.assertEqual(self.enqueued, [3])
        with mock.patch.object(models.SpikeEscalation, "for_pair",
                               return_value=_FakeEscalation(id=3, status="error", attempts=2)):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)

    def test_the_daily_run_budget_binds(self):
        with mock.patch.object(models.SpikeEscalation, "count_since",
                               return_value=self.cfg["max_runs_per_day"]):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)
        self.assertEqual(self.created, [])

    def test_the_tick_room_binds(self):
        rows = [_row(32, [1, 0, 2], 21, signature="A::a"), _row(40, [0, 0, 0], 30, signature="B::b")]
        self.assertEqual(self._sweep(rows, room=1), 1)

    def test_a_lambdas_two_demanglings_are_one_spike(self):
        rows = [_row(9, [1, 0, 0], 6, signature="Q::Shutdown::<T>::operator()"),
                _row(8, [0, 1, 0], 5, signature="Q::Shutdown::$::operator()")]
        # Split, neither half clears the excess test (9 vs bar 9 on baseline 1 passes the ratio
        # but 8 does not); merged: 17 over [1, 1, 0], 11 installs.
        self.assertEqual(self._sweep(rows), 1)
        row = self.created[0]
        self.assertEqual(row.payload["spike"]["count"], 17)
        self.assertEqual(set(row.payload["siblings"]),
                         {"Q::Shutdown::<T>::operator()", "Q::Shutdown::$::operator()"})

    def test_no_stack_bearing_report_is_recorded_not_enqueued(self):
        with mock.patch.object(se, "representative_uuid", return_value=None):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)
        self.assertEqual(self.created[0].status, "done")
        self.assertIn("no ingested report", self.created[0].payload["skipped"])

    def test_the_sweep_is_off_with_the_switch(self):
        with mock.patch.object(config, "get_agent_spike_escalation",
                               return_value=dict(self.cfg, enabled=False)), \
                mock.patch.object(models.Selection, "escalation_candidates") as cands:
            self.assertEqual(se.sweep_real_spikes(), 0)
        cands.assert_not_called()


class TestSummarizeRun(unittest.TestCase):
    def test_the_ordinary_runs_conclusions_are_compacted(self):
        payload = {"dossier": {"verdict": {"decision": "abstain", "confidence": "low",
                                           "abstain_kind": "pre_existing",
                                           "abstain_reason": "old " * 400,
                                           "mechanism": {"statement": "m"}},
                               "candidate": {"node": "abc", "bug": 1},
                               "second_opinion": {"corroborates": False, "refutation": "r"}},
                   "filing_declined": {"skipped": "confidence 25 below 70"},
                   "filed_bug": {"filed": False, "skipped": "x"}}
        run = se.summarize_run("u-1", "done", payload)
        self.assertEqual((run["verdict"], run["abstain_kind"]), ("abstain", "pre_existing"))
        self.assertTrue(run["abstain_reason"].endswith("..."))
        self.assertEqual(run["candidate"], {"node": "abc", "bug": 1})
        self.assertEqual(run["second_opinion"]["corroborates"], False)
        self.assertIsNone(run["filed_bug"], "a recorded skip is not a filing")
        self.assertEqual(run["declined"], "confidence 25 below 70")
        lines = spike_agent._classic_run_lines(run)
        self.assertTrue(any("REFUTED" in line for line in lines))


class TestTheBugText(unittest.TestCase):
    _BRIEF = {"signature": "Foo::Bar", "channel": "nightly", "product": "Firefox",
              "version": "157.0a1", "buildid": "20260903093145", "build_day": "2026-09-03",
              "uuid": "u-1", "first_seen_ever": "20250101000000",
              "spike": {"kind": "build_day", "count": 32, "installs": 21, "baseline": [1, 0, 2],
                        "ratio": 16.0, "z": 7.3, "z_min": 3.62},
              "trend_sentence": "45 distinct installations in the last 7 days (67 reports), ...",
              "version_step": {"version": "155.0.1", "from_version": "155.0", "ratio": 5.5},
              "stacks": [{"uuid": "u-1"}, {"uuid": "u-2"}, {"uuid": "u-1"}]}

    def test_the_spike_paragraph(self):
        text = spike_report.spike_paragraph(self._BRIEF)
        self.assertTrue(text.startswith("**Filed because this signature's crash volume spiked.**"))
        for piece in ("32 reports from 21 distinct installations", "16.0x", "z = 7.3",
                      "45 distinct installations in the last 7 days", "155.0.1 runs at 5.5x",
                      "not new", "2025-01-01"):
            self.assertIn(piece, text)

    def test_a_new_signature_says_so(self):
        text = spike_report.signature_age_sentence(dict(self._BRIEF, first_seen_ever="20260903093145"))
        self.assertIn("is new", text)
        # New on the channel, old elsewhere: both facts, in that order.
        text = spike_report.signature_age_sentence(
            dict(self._BRIEF, channel="release", first_seen_channel="20260903093145"))
        self.assertIn("new on release", text)
        self.assertIn("first report anywhere is in build 20250101000000", text)
        # Old on the channel too.
        text = spike_report.signature_age_sentence(
            dict(self._BRIEF, channel="release", first_seen_channel="20260101000000"))
        self.assertIn("not new", text)
        self.assertNotIn("new on release", text)

    def test_an_appearance_earns_the_channel_prefix_and_a_rise_does_not(self):
        appearance = dict(self._BRIEF, channel="release",
                          spike={"kind": "build_day", "count": 50, "installs": 50,
                                 "baseline": [0, 0, 0, 0]},
                          first_seen_channel="20260903093145")
        self.assertTrue(spike_report.is_new_signature(appearance))
        p = spike_report.build_spike_preview(appearance, None, product="Core", component="General")
        self.assertEqual(p["title"], "[new in release] Crash in [@ Foo::Bar]")
        # The same shape with an earlier report on release: an old signature come back.
        self.assertFalse(spike_report.is_new_signature(
            dict(appearance, first_seen_channel="20260801000000")))
        # A rise over a non-zero baseline is never an appearance, whatever the clock says.
        self.assertFalse(spike_report.is_new_signature(dict(self._BRIEF, channel="release")))
        # Nor is a rate-path spike.
        self.assertFalse(spike_report.is_new_signature(
            dict(appearance, spike={"kind": "rate", "installs": 45})))
        # A failed channel lookup cannot make an appearance look old: new until a clock says so.
        self.assertTrue(spike_report.is_new_signature(dict(appearance, first_seen_channel=None)))
        # Nightly and beta have no prefix policy, so an appearance there stays bare.
        p = spike_report.build_spike_preview(dict(appearance, channel="nightly"), None,
                                             product="Core", component="General")
        self.assertEqual(p["title"], "Crash in [@ Foo::Bar]")

    def test_regression_and_regressed_by_follow_the_confidence(self):
        low = SpikeFindings(summary="S", culprit={"node": "abcdef0", "bug": 5, "confidence": "low"})
        p = spike_report.build_spike_preview(self._BRIEF, low, product="Core", component="General",
                                             link_regressor=True)
        self.assertEqual((p["keywords"], p["regressed_by"]), (["crash"], []))
        medium = SpikeFindings(summary="S", culprit={"node": "abcdef0", "bug": 5, "confidence": "medium"})
        p = spike_report.build_spike_preview(self._BRIEF, medium, product="Core",
                                             component="General", link_regressor=True)
        self.assertEqual((p["keywords"], p["regressed_by"]), (["crash", "regression"], []))
        high = SpikeFindings(summary="S", culprit={"node": "abcdef0", "bug": 5, "confidence": "high"})
        p = spike_report.build_spike_preview(self._BRIEF, high, product="Core",
                                             component="General", link_regressor=True)
        self.assertEqual(p["regressed_by"], [5])
        p = spike_report.build_spike_preview(self._BRIEF, high, product="Core",
                                             component="General", link_regressor=False)
        self.assertEqual((p["keywords"], p["regressed_by"]), (["crash", "regression"], []))

    def test_other_reports_are_linked_once_each(self):
        line = spike_report.other_reports_line(self._BRIEF)
        self.assertEqual(line.count("u-2"), 1)
        self.assertNotIn("u-1", line)

    def test_the_analysis_lists_are_bounded(self):
        f = SpikeFindings(summary="S", evidence=[{"claim": str(i), "source": "s"} for i in range(20)],
                          ruled_out=[str(i) for i in range(20)])
        text = spike_report.analysis_section(f, self._BRIEF)
        bullets = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(bullets), spike_report._MAX_EVIDENCE + spike_report._MAX_LIST)


class TestCrashStatsTools(unittest.TestCase):
    def test_facets_refuses_an_unknown_field(self):
        out = asyncio.run(crashstats.facets(crashstats.CrashStatsCtx(), "sig", "user_comments"))
        self.assertIn("not a field this tool will facet", out)
        self.assertIn("platform_pretty_version", out)

    def test_facets_splits_at_the_spike_build(self):
        calls = []

        def search(params):
            calls.append(params)
            return {"total": 10, "facets": {"process_type": [{"term": "content", "count": 8},
                                                             {"term": "parent", "count": 2}]}}
        with mock.patch.object(crashstats, "_search", side_effect=search):
            out = asyncio.run(crashstats.facets(
                crashstats.CrashStatsCtx(channel="beta"), "sig", "process_type",
                split_at_build="20260903093145"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["build_id"], ["<20260903093145"])
        self.assertEqual(calls[1]["build_id"], [">=20260903093145"])
        self.assertEqual(calls[0]["release_channel"], ["beta", "aurora"])
        self.assertIn("BEFORE build 20260903093145", out)
        self.assertIn("FROM build 20260903093145 on", out)
        self.assertIn("content: 8 (80%)", out)
        out = asyncio.run(crashstats.facets(crashstats.CrashStatsCtx(), "sig", "process_type",
                                            split_at_build="2026"))
        self.assertIn("must be a 14-digit buildid", out)

    def test_a_numeric_field_is_bucketed(self):
        calls = []

        def search(params):
            calls.append(params)
            return {"total": 3, "facets": {"histogram_uptime": [{"term": 0, "count": 2},
                                                                {"term": 60, "count": 1}]}}
        with mock.patch.object(crashstats, "_search", side_effect=search):
            out = asyncio.run(crashstats.facets(crashstats.CrashStatsCtx(), "sig", "uptime",
                                                interval="60"))
        self.assertEqual(calls[0]["_histogram_interval.uptime"], "60")
        self.assertIn("0: 2 (67%)", out)

    def test_report_prints_the_threads_and_one_stack(self):
        raw = {"product": "Firefox", "version": "157.0a1", "report_type": "hang",
               "shutdown_progress": "profile-before-change", "uptime": 80,
               "json_dump": {"crash_info": {"crashing_thread": 1, "type": "SIGABRT"},
                             "threads": [
                                 {"thread_name": "MainThread", "frames": [
                                     {"function": "Wait", "file": "hg:hg.mozilla.org/mozilla-central:xpcom/threads/x.cpp:abc", "line": 5, "module": "xul.dll"}]},
                                 {"thread_name": "Shutdown Hang Terminator", "frames": [
                                     {"function": "RunWatchdog", "module": "xul.dll"}]}]},
               "crashing_thread": 0}
        with mock.patch.object(crashstats.inspector, "get_crash_data", return_value=raw):
            out = asyncio.run(crashstats.report(crashstats.CrashStatsCtx(), "u-1"))
            other = asyncio.run(crashstats.report(crashstats.CrashStatsCtx(), "u-1", thread=1))
        self.assertIn("shutdown_progress: profile-before-change", out)
        self.assertIn("threads (2)", out)
        self.assertIn("thread 0 (MainThread) stack", out, "the hung thread, not the watchdog")
        self.assertIn("xpcom/threads/x.cpp:5", out)
        self.assertIn("thread 1 (Shutdown Hang Terminator) stack", other)
        self.assertIn("RunWatchdog", other)


if __name__ == "__main__":
    unittest.main()
