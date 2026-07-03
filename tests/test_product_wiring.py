# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Product wiring (#12): evidence panel, /api/evidence(+apply), and the apply/replay
# step. Runs with no real DB / no real network:
#   DATABASE_URL=sqlite:// python -m unittest tests.test_product_wiring
# The model accessors and the Bugzilla REST client are mocked; no test performs a
# real Bugzilla write (asserted explicitly).
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import unittest  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from flask import render_template  # noqa: E402

from crashclouseau import app, bugzilla_apply, html, report_bug  # noqa: E402


_SEARCHFOX = {
    "kind": "searchfox",
    "permalink": "https://searchfox.org/mozilla-central/rev/deadbeef#42",
    "symbol_id": "_ZN3Foo3barEv",
    "repo": "mozilla-central",
    "rev": "deadbeefcafe1234",
}
_DIFF = {
    "kind": "diff_line",
    "node": "culpritnode1",
    "filename": "dom/Foo.cpp",
    "line": 42,
    "side": "added",
    "content": "delete mPtr;",
}
_FRAME = {
    "kind": "stack_frame",
    "uuid": "u",
    "stackpos": 0,
    "filename": "dom/Foo.cpp",
    "function": "Foo::bar",
    "line": 51,
    "node": "buildnode1234",
}


def _dossier():
    return {
        "crash": {"failure_class": "uaf", "moz_crash_reason": None},
        "candidate": {
            "node": "culpritnode1",
            "bug": 99999,
            "author": "Dev One <dev@moz.example>",
            "backedout": False,
            "changed_functions": ["Foo::bar"],
        },
        "call_path": {
            "to_symbol": "Foo::bar",
            "edges": [
                {
                    "caller_symbol": "A::run",
                    "callee_symbol": "Foo::bar",
                    "via": "calls-to",
                    "citations": [_SEARCHFOX],
                }
            ],
        },
        "hunks": [
            {
                "node": "culpritnode1",
                "filename": "dom/Foo.cpp",
                "header": "@@ -40,6 +40,7 @@",
                "lines": [_DIFF],
                "citations": [],
            }
        ],
        "data_flow": {
            "summary": "mPtr freed then dereferenced",
            "operation": "uaf",
            "object_name": "mPtr",
            "citations": [_DIFF],
            "crash_site": _FRAME,
        },
        "skeptic": [
            {"claim_ref": "mechanism", "status": "pass", "note": "verified"},
            {"claim_ref": "regressor", "status": "fail", "note": "backout unclear"},
        ],
        "verdict": {
            "decision": "strong-evidence",
            "confidence": "high",
            "mechanism": {"statement": "use-after-free of mPtr", "citations": [_SEARCHFOX]},
            "consistency": {"statement": "matches the crashing frame", "citations": [_FRAME]},
        },
    }


def _actions():
    return [
        {
            "type": "bugzilla.add_comment",
            "params": {"bug_id": 99999, "text": "Clouseau analysis...", "is_private": False},
            "reasoning": "share the evidence chain",
        },
        {
            "type": "bugzilla.update_bug",
            "params": {
                "bug_id": 99999,
                "changes": {
                    "flags": [{"name": "needinfo", "status": "?", "requestee": "dev@moz.example"}]
                },
            },
            "reasoning": "ask the patch author",
        },
        {
            "type": "bugzilla.add_comment",
            "params": {"bug_id": 99999, "text": "already posted", "is_private": False},
            "reasoning": "prior comment",
            "applied_at": "2026-07-01T00:00:00+00:00",
            "result_id": 5,
        },
    ]


def _evidence(verdict="culprit", confidence=90, show_abstain=False):
    ui = {
        "show_abstain": show_abstain,
        "high_confidence_label": "STRONG EVIDENCE",
        "apply_min_confidence": 85,
        "enabled_types": ["bugzilla.add_comment", "bugzilla.update_bug"],
    }
    actions = _actions()
    idxs = bugzilla_apply.applicable_indices(actions, ui)
    return {
        "uuid": "u-1",
        "verdict": verdict,
        "confidence": confidence,
        "principal_model": "claude-opus-4-8",
        "rationale": "insufficient" if verdict == "abstain" else "",
        "evidence": [],
        "effort": "high",
        "dossier": _dossier(),
        "actions": actions,
        "over_budget": False,
        "status": "done",
        "cost_usd": 1.23,
        "ui": ui,
        "apply_indices": idxs,
        "can_apply": bool(verdict == "culprit" and confidence >= 85 and idxs),
    }


def _uuid_info():
    return {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "id": 1,
        "signature": "Foo::bar",
        "buildid": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "channel": "nightly",
        "product": "Firefox",
        "java": False,
        "node": "buildnode1234",
    }


def _stack():
    return {
        "frames": [
            {
                "stackpos": 0,
                "filename": "dom/Foo.cpp",
                "function": "Foo::bar",
                "changesets": OrderedDict(),
                "line": 51,
                "node": "buildnode1234",
                "original": "Foo::bar",
                "internal": True,
                "url": "https://hg.example/file",
            }
        ]
    }


class TestEvidenceApi(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_missing_uuid_400(self):
        rv = self.client.get("/api/evidence")
        self.assertEqual(rv.status_code, 400)

    def test_no_verdict_returns_null(self):
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=None):
            rv = self.client.get("/api/evidence?uuid=u-1")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {"uuid": "u-1", "verdict": None})

    def test_returns_evidence_json_and_writes_nothing(self):
        # Mock at the DB accessor so the REAL evidence() -> build_evidence ->
        # applicable_indices/gate path runs (non-tautological): can_apply below is
        # COMPUTED by the product code, and no Bugzilla request may fire on a read.
        raw = {
            "uuid": "u-1", "verdict": "culprit", "confidence": 90,
            "principal_model": "m", "rationale": "", "evidence": [], "effort": "high",
            "dossier": _dossier(), "actions": _actions(),
            "over_budget": False, "status": "done", "cost_usd": 1.0,
        }
        with mock.patch("crashclouseau.models.Verdict.get_evidence", return_value=raw), \
                mock.patch.object(bugzilla_apply, "requests") as req:
            rv = self.client.get("/api/evidence?uuid=u-1")
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertEqual(body["verdict"], "culprit")
        self.assertTrue(body["can_apply"])
        self.assertEqual(body["apply_indices"], [0, 1])
        req.post.assert_not_called()
        req.put.assert_not_called()


class TestCrashstackPanel(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def _get(self, evidence):
        with mock.patch("crashclouseau.models.CrashStack.get_by_uuid",
                        return_value=(_stack(), _uuid_info())), \
                mock.patch.object(bugzilla_apply, "build_evidence", return_value=evidence):
            return self.client.get("/crashstack.html?uuid=u-1")

    def test_unknown_uuid_404(self):
        with mock.patch("crashclouseau.models.CrashStack.get_by_uuid",
                        return_value=({}, {})):
            rv = self.client.get("/crashstack.html?uuid=missing")
        self.assertEqual(rv.status_code, 404)

    def test_no_verdict_renders_without_panel(self):
        rv = self._get(None)
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertNotIn("evidence-panel", html)

    def test_culprit_panel_full(self):
        rv = self._get(_evidence())
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("evidence-panel", html)
        self.assertIn("STRONG EVIDENCE", html)
        self.assertIn("90%", html)
        # citations
        self.assertIn(_SEARCHFOX["permalink"], html)
        # diff-line citations + hunk file link now point at the two-pane code view,
        # carrying the page's UUID (uuid_info), the file, node, and line.
        self.assertIn("/codeview.html?uuid=", html)
        self.assertIn("filename=dom/Foo.cpp", html)
        self.assertIn("node=culpritnode1", html)
        # the build revision (uuid_info node) + channel are threaded for the searchfox pin
        self.assertIn("rev=buildnode1234", html)
        self.assertIn("channel=nightly", html)
        self.assertNotIn("/diff.html", html)
        self.assertNotIn("/rev?node=", html)
        # culprit + call path + data flow + skeptic
        self.assertIn("bug 99999", html)
        self.assertIn("A::run", html)
        self.assertIn("use-after-free of mPtr", html)
        self.assertIn("skeptic-fail", html)
        # audit trail
        self.assertIn("Recorded Bugzilla actions", html)
        self.assertIn("dev@moz.example", html)
        self.assertIn("share the evidence chain", html)
        # apply control + selectable checkboxes for the two unapplied actions
        self.assertIn("applyActionsBtn", html)
        self.assertEqual(html.count('class="apply-cb"'), 2)
        # the already-applied action shows the applied marker, not a checkbox
        self.assertIn("applied 2026-07-01", html)

    def test_panel_tolerates_null_action_entry(self):
        # A null/dropped entry in payload["actions"] (schema drift) must not 500 the
        # whole page; the apply path guards it, and so must the template.
        ev = _evidence()
        ev["actions"] = [None] + _actions()
        ev["apply_indices"] = bugzilla_apply.applicable_indices(ev["actions"], ev["ui"])
        rv = self._get(ev)
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Recorded Bugzilla actions", rv.get_data(as_text=True))

    def test_searchfox_permalink_scheme_allowlist(self):
        # A javascript: permalink must NOT become a clickable href (XSS guard).
        ev = _evidence()
        bad = dict(ev["dossier"]["verdict"]["mechanism"])
        bad["citations"] = [dict(_SEARCHFOX, permalink="javascript:alert(document.domain)")]
        ev["dossier"]["verdict"]["mechanism"] = bad
        html = self._get(ev).get_data(as_text=True)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("href=\"javascript:alert", html)

    def test_call_path_symbols_demangled(self):
        ev = _evidence()
        ev["dossier"]["call_path"]["edges"][0]["caller_symbol"] = "_ZN3Foo3BarEv"
        ev["dossier"]["call_path"]["edges"][0]["callee_symbol"] = "_Z1fv"
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("Foo::Bar()", html)               # demangled shown
        self.assertIn("f()", html)
        self.assertIn('title="_ZN3Foo3BarEv"', html)     # mangled kept as hover title

    def test_abstain_hidden_by_default(self):
        rv = self._get(_evidence(verdict="abstain", confidence=20))
        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("evidence-panel", rv.get_data(as_text=True))

    def test_abstain_shown_when_configured(self):
        ev = _evidence(verdict="abstain", confidence=20, show_abstain=True)
        rv = self._get(ev)
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("evidence-panel", html)
        self.assertIn("ABSTAIN", html)
        # abstain never offers an apply control
        self.assertNotIn("applyActionsBtn", html)


class TestApplyRecordedActions(unittest.TestCase):
    """The apply/replay step: bounded by enabled_types, idempotent, mocked REST."""

    def _run(self, indices, actions=None, token="TESTTOKEN",
             verdict="culprit", confidence=90):
        actions = _actions() if actions is None else actions
        ev = {"verdict": verdict, "confidence": confidence, "actions": actions}
        with mock.patch.object(bugzilla_apply, "requests") as req, \
                mock.patch.object(bugzilla_apply.libmozdata.config, "get",
                                  return_value=token), \
                mock.patch("crashclouseau.models.Verdict.get_evidence",
                           return_value=ev), \
                mock.patch("crashclouseau.models.Dossier.mark_action_applied") as marked:
            req.post.return_value.json.return_value = {"id": 111}
            req.put.return_value.json.return_value = {"bugs": [{"id": 99999}]}
            results = bugzilla_apply.apply_recorded_actions("u-1", indices)
        return results, req, marked

    def test_add_comment_executes(self):
        results, req, marked = self._run([0])
        self.assertEqual(results[0]["ok"], True)
        self.assertEqual(results[0]["result_id"], 111)
        args, kwargs = req.post.call_args
        self.assertIn("/rest/bug/99999/comment", args[0])
        self.assertEqual(kwargs["headers"]["X-Bugzilla-API-Key"], "TESTTOKEN")
        self.assertEqual(kwargs["json"]["comment"], "Clouseau analysis...")
        marked.assert_called_once_with("u-1", 0, 111)

    def test_update_bug_needinfo_executes(self):
        results, req, marked = self._run([1])
        self.assertTrue(results[0]["ok"])
        args, kwargs = req.put.call_args
        self.assertTrue(args[0].endswith("/rest/bug/99999"))
        self.assertEqual(kwargs["json"]["flags"][0]["requestee"], "dev@moz.example")
        marked.assert_called_once_with("u-1", 1, 99999)

    def test_already_applied_is_skipped(self):
        results, req, marked = self._run([2])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["skipped"], "already applied")
        self.assertEqual(results[0]["result_id"], 5)
        req.post.assert_not_called()
        req.put.assert_not_called()
        marked.assert_not_called()

    def test_type_not_enabled_refused(self):
        actions = [
            {"type": "bugzilla.add_attachment",
             "params": {"bug_id": 99999, "file_name": "x.patch"},
             "reasoning": "r"}
        ]
        results, req, marked = self._run([0], actions=actions)
        self.assertFalse(results[0]["ok"])
        self.assertIn("not enabled", results[0]["error"])
        req.post.assert_not_called()
        req.put.assert_not_called()
        marked.assert_not_called()

    def test_enabled_types_bound_independent_of_executable(self):
        # add_comment IS executable but here NOT in enabled_types -> refused by the
        # config-driven bound alone (isolates it from the _EXECUTABLE check).
        ui = {"show_abstain": False, "high_confidence_label": "X",
              "apply_min_confidence": 85, "enabled_types": ["bugzilla.update_bug"]}
        with mock.patch.object(bugzilla_apply.config, "get_agent_ui", return_value=ui), \
                mock.patch.object(bugzilla_apply, "requests") as req, \
                mock.patch.object(bugzilla_apply.libmozdata.config, "get",
                                  return_value="TESTTOKEN"), \
                mock.patch("crashclouseau.models.Verdict.get_evidence",
                           return_value={"verdict": "culprit", "confidence": 90,
                                         "actions": _actions()}), \
                mock.patch("crashclouseau.models.Dossier.mark_action_applied") as marked:
            results = bugzilla_apply.apply_recorded_actions("u-1", [0])
        self.assertFalse(results[0]["ok"])
        self.assertIn("not enabled", results[0]["error"])
        req.post.assert_not_called()
        marked.assert_not_called()

    def test_partial_failure_marks_only_the_success(self):
        # action 0 (comment) succeeds; action 1 (update_bug PUT) raises. The earlier
        # success must stay marked-applied so a retry never re-posts it.
        with mock.patch.object(bugzilla_apply, "requests") as req, \
                mock.patch.object(bugzilla_apply.libmozdata.config, "get",
                                  return_value="TESTTOKEN"), \
                mock.patch("crashclouseau.models.Verdict.get_evidence",
                           return_value={"verdict": "culprit", "confidence": 90,
                                         "actions": _actions()}), \
                mock.patch("crashclouseau.models.Dossier.mark_action_applied") as marked:
            req.post.return_value.json.return_value = {"id": 111}
            req.put.side_effect = RuntimeError("bugzilla 500")
            results = bugzilla_apply.apply_recorded_actions("u-1", [0, 1])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["result_id"], 111)
        self.assertFalse(results[1]["ok"])
        self.assertIn("bugzilla 500", results[1]["error"])
        marked.assert_called_once_with("u-1", 0, 111)

    def test_ineligible_verdict_refused_without_write(self):
        # A hand-crafted POST for a low-confidence or abstain verdict is refused
        # server-side (defense-in-depth), before any Bugzilla call.
        for kwargs in ({"confidence": 50}, {"verdict": "abstain"}):
            results, req, marked = self._run([0, 1], **kwargs)
            self.assertTrue(all(not r["ok"] for r in results))
            self.assertIn("not eligible", results[0]["error"])
            req.post.assert_not_called()
            req.put.assert_not_called()
            marked.assert_not_called()

    def test_missing_token_fails_without_write(self):
        results, req, marked = self._run([0, 1], token="")
        self.assertFalse(results[0]["ok"])
        self.assertIn("token", results[0]["error"])
        req.post.assert_not_called()
        req.put.assert_not_called()
        marked.assert_not_called()

    def test_create_bug_routes_to_draft(self):
        actions = [
            {"type": "bugzilla.create_bug",
             "params": {"product": "Firefox", "component": "DOM"},
             "reasoning": "file it"}
        ]
        results, req, marked = self._run([0], actions=actions)
        self.assertTrue(results[0]["ok"])
        self.assertIn("draft_url", results[0])
        req.post.assert_not_called()
        req.put.assert_not_called()

    def test_bad_index(self):
        results, _, _ = self._run([7])
        self.assertFalse(results[0]["ok"])
        self.assertIn("no such action", results[0]["error"])

    def test_duplicate_indices_execute_once(self):
        # A repeated index in one request must not double-post the same action.
        results, req, marked = self._run([0, 0, 0])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(req.post.call_count, 1)
        marked.assert_called_once_with("u-1", 0, 111)

    def test_no_verdict_raises_lookup(self):
        with mock.patch("crashclouseau.models.Verdict.get_evidence", return_value=None):
            with self.assertRaises(LookupError):
                bugzilla_apply.apply_recorded_actions("u-1", [0])


class TestBuildEvidence(unittest.TestCase):
    """The REAL can_apply / confidence gate (Verdict.get_evidence mocked at the DB)."""

    def _raw(self, verdict="culprit", confidence=90, actions=None):
        return {
            "uuid": "u-1", "verdict": verdict, "confidence": confidence,
            "principal_model": "m", "rationale": "", "evidence": [], "effort": "high",
            "dossier": _dossier(), "actions": _actions() if actions is None else actions,
            "over_budget": False, "status": "done", "cost_usd": 1.0,
        }

    def _build(self, **kw):
        with mock.patch("crashclouseau.models.Verdict.get_evidence",
                        return_value=self._raw(**kw)):
            return bugzilla_apply.build_evidence("u-1")

    def test_can_apply_true_for_high_culprit(self):
        ev = self._build()
        self.assertTrue(ev["can_apply"])
        self.assertEqual(ev["apply_indices"], [0, 1])

    def test_can_apply_false_for_medium_confidence(self):
        self.assertFalse(self._build(confidence=50)["can_apply"])

    def test_can_apply_false_for_abstain(self):
        self.assertFalse(self._build(verdict="abstain", confidence=90)["can_apply"])

    def test_none_when_no_verdict(self):
        with mock.patch("crashclouseau.models.Verdict.get_evidence", return_value=None):
            self.assertIsNone(bugzilla_apply.build_evidence("u-1"))

    def test_all_applied_yields_no_apply(self):
        acts = [dict(a, applied_at="t", result_id=1) for a in _actions()]
        ev = self._build(actions=acts)
        self.assertEqual(ev["apply_indices"], [])
        self.assertFalse(ev["can_apply"])


class TestApplyRoute(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_missing_uuid_400(self):
        rv = self.client.post("/api/evidence/apply", json={"indices": [0]})
        self.assertEqual(rv.status_code, 400)

    def test_bad_indices_400(self):
        rv = self.client.post("/api/evidence/apply", json={"uuid": "u-1", "indices": "x"})
        self.assertEqual(rv.status_code, 400)

    def test_boolean_indices_rejected_400(self):
        # JSON booleans are ints in Python; they must not slip through as indices.
        rv = self.client.post("/api/evidence/apply",
                              json={"uuid": "u-1", "indices": [True]})
        self.assertEqual(rv.status_code, 400)

    def test_ok(self):
        payload = [{"index": 0, "type": "bugzilla.add_comment", "ok": True, "result_id": 111}]
        with mock.patch.object(bugzilla_apply, "apply_recorded_actions",
                               return_value=payload) as ap:
            rv = self.client.post("/api/evidence/apply",
                                  json={"uuid": "u-1", "indices": [0]})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {"uuid": "u-1", "results": payload})
        ap.assert_called_once_with("u-1", [0])

    def test_unknown_uuid_404(self):
        with mock.patch.object(bugzilla_apply, "apply_recorded_actions",
                               side_effect=LookupError):
            rv = self.client.post("/api/evidence/apply",
                                  json={"uuid": "u-1", "indices": [0]})
        self.assertEqual(rv.status_code, 404)


class TestReportBugEvidenceSummary(unittest.TestCase):
    """finalize_comment stays byte-identical when the summary is omitted (#12)."""

    def _finalize(self, evidence_summary):
        bzquery = {"comment": ["socorro comment text"]}
        stats = {"count": 3, "installs": 2}
        info = {"channel": "release", "version": "120.0", "buildid": "20240101000000"}
        report_bug.finalize_comment(
            bzquery, True, stats, info, "abc123", 99999,
            evidence_summary=evidence_summary,
        )
        return bzquery["comment"]

    def test_none_path_has_no_evidence_and_append_is_additive(self):
        # Non-tautological: the None path must carry NO evidence block, and providing
        # a summary must be purely additive (base unchanged + appended tail). A
        # regression that appended any constant footer on the None path would make
        # `base` contain it and break the additive equality below.
        base = self._finalize(None)
        self.assertNotIn("Clouseau evidence", base)
        summary = "Clouseau evidence (confidence high): uaf of mPtr."
        self.assertEqual(self._finalize(summary), base.rstrip("\n") + "\n\n" + summary + "\n")

    def test_appended_when_provided(self):
        base = self._finalize(None)
        summary = "Clouseau evidence (confidence high): uaf of mPtr."
        with_summary = self._finalize(summary)
        self.assertNotIn(summary, base)
        self.assertIn(summary, with_summary)
        self.assertTrue(with_summary.startswith(base.rstrip("\n")))


class TestCodeview(unittest.TestCase):
    """The two-pane code view (#12): searchfox iframe + persisted diff lines."""

    def setUp(self):
        self.client = app.test_client()

    def test_collect_diff_lines_gathers_and_sorts(self):
        from crashclouseau import html
        dossier = _dossier()  # data_flow + hunk carry diff_line citations for dom/Foo.cpp
        lines = html._collect_diff_lines(dossier, "dom/Foo.cpp")
        self.assertTrue(lines)
        self.assertTrue(all(item["side"] in ("added", "deleted", "context") for item in lines))
        self.assertEqual(lines, sorted(lines, key=lambda d: d["line"]))
        # a file with no citations yields nothing
        self.assertEqual(html._collect_diff_lines(dossier, "no/such/File.cpp"), [])

    def test_codeview_renders_two_panes(self):
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=_evidence()):
            rv = self.client.get(
                "/codeview.html?uuid=u-1&filename=dom/Foo.cpp&node=culpritnode1&line=42"
            )
        self.assertEqual(rv.status_code, 200)
        html_text = rv.get_data(as_text=True)
        # left pane: searchfox iframe of the file; right pane: the persisted diff line
        self.assertIn('id="sf"', html_text)
        self.assertIn('name="sf"', html_text)
        self.assertIn('src="https://searchfox.org/firefox-main/source/dom/Foo.cpp#line-42"', html_text)
        self.assertIn("delete mPtr;", html_text)          # the _DIFF diff_line content
        self.assertIn("dl-added", html_text)
        # clicking a patch line navigates the named searchfox frame to that line
        # (searchfox anchors lines as id="line-<n>", so the fragment is #line-<n>)
        self.assertIn(
            'href="https://searchfox.org/firefox-main/source/dom/Foo.cpp#line-42" target="sf"',
            html_text,
        )
        # never falls back to the slow hg/gh web viewers
        self.assertNotIn("hg.mozilla.org", html_text)
        self.assertNotIn("github.com", html_text)

    def test_codeview_without_evidence_is_searchfox_only(self):
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=None):
            rv = self.client.get("/codeview.html?uuid=x&filename=a/b.cpp")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("https://searchfox.org/firefox-main/source/a/b.cpp", rv.get_data(as_text=True))

    def test_codeview_pins_to_build_rev_on_channel_tree(self):
        # beta channel -> firefox-beta tree; rev -> /rev/<buildrev>/ (source as built).
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=_evidence()):
            rv = self.client.get(
                "/codeview.html?uuid=u-1&filename=dom/Foo.cpp&node=culpritnode1"
                "&line=42&channel=beta&rev=d0d4ceee5e1c"
            )
        self.assertEqual(rv.status_code, 200)
        t = rv.get_data(as_text=True)
        self.assertIn(
            'src="https://searchfox.org/firefox-beta/rev/d0d4ceee5e1c/dom/Foo.cpp#line-42"',
            t,
        )
        self.assertIn(
            'href="https://searchfox.org/firefox-beta/rev/d0d4ceee5e1c/dom/Foo.cpp#line-42" target="sf"',
            t,
        )
        self.assertNotIn("/source/", t)   # pinned to the rev, not tip

    def test_searchfox_tree_by_channel(self):
        self.assertEqual(html._searchfox_tree("nightly"), "firefox-main")
        self.assertEqual(html._searchfox_tree("beta"), "firefox-beta")
        self.assertEqual(html._searchfox_tree("release"), "firefox-release")
        self.assertEqual(html._searchfox_tree("aurora"), "firefox-main")  # unknown -> default


class TestDraftEvidence(unittest.TestCase):
    """html._draft_evidence + the preserved enter_bug draft (#12)."""

    def test_culprit_matching_changeset(self):
        with mock.patch("crashclouseau.models.Verdict.get_evidence",
                        return_value=_evidence()):
            summary, ni, author = html._draft_evidence("u-1", "culpritnode1")
        self.assertIn("use-after-free of mPtr", summary)
        self.assertIn("Suspected regressor: culpritnode1", summary)
        self.assertEqual(ni, "dev@moz.example")
        self.assertEqual(author, "Dev One <dev@moz.example>")

    def test_non_matching_changeset_no_author(self):
        with mock.patch("crashclouseau.models.Verdict.get_evidence",
                        return_value=_evidence()):
            summary, ni, author = html._draft_evidence("u-1", "otherchangeset")
        self.assertIsNotNone(summary)   # summary still surfaced
        self.assertIsNone(ni)           # but not this changeset's author
        self.assertIsNone(author)

    def test_abstain_is_empty(self):
        with mock.patch("crashclouseau.models.Verdict.get_evidence",
                        return_value=_evidence(verdict="abstain", confidence=10)):
            self.assertEqual(
                html._draft_evidence("u-1", "culpritnode1"), (None, None, None)
            )

    def test_no_verdict_is_empty(self):
        with mock.patch("crashclouseau.models.Verdict.get_evidence", return_value=None):
            self.assertEqual(
                html._draft_evidence("u-1", "culpritnode1"), (None, None, None)
            )

    def test_bug_html_renders_draft_block(self):
        with app.test_request_context("/bug.html"):
            out = render_template(
                "bug.html", uuid="u-1", url="https://bugzilla.mozilla.org/enter_bug.cgi?x=1",
                needinfo="assignee@moz.example", bugdata=[], signature="Foo::bar",
                evidence_summary="Clouseau evidence (confidence high): uaf.",
                culprit_author="Dev One <dev@moz.example>", culprit_ni="dev@moz.example",
            )
        self.assertIn("evidence-draft", out)
        self.assertIn("Clouseau evidence (confidence high): uaf.", out)
        self.assertIn('mailto:dev@moz.example', out)
        self.assertIn('mailto:assignee@moz.example', out)  # existing ni preserved

    def test_bug_html_no_draft_block_when_absent(self):
        with app.test_request_context("/bug.html"):
            out = render_template(
                "bug.html", uuid="u-1", url="https://bugzilla.mozilla.org/enter_bug.cgi?x=1",
                needinfo="", bugdata=[], signature="Foo::bar",
                evidence_summary=None, culprit_author=None, culprit_ni=None,
            )
        self.assertNotIn("evidence-draft", out)


class TestDemangle(unittest.TestCase):
    def test_valid_symbol_demangled(self):
        from crashclouseau import utils
        self.assertEqual(utils.demangle("_ZN3Foo3BarEv"), "Foo::Bar()")
        self.assertEqual(utils.demangle("_Z1fv"), "f()")

    def test_non_symbol_falls_back_unchanged(self):
        from crashclouseau import utils
        self.assertEqual(utils.demangle(""), "")
        self.assertEqual(utils.demangle("A::run"), "A::run")   # already readable
        # off-by-one corrupted mangled id -> cpp_demangle raises -> unchanged
        bad = "_ZN2js8GCMarker27markCurrentColorInParallelEv"
        self.assertEqual(utils.demangle(bad), bad)


if __name__ == "__main__":
    unittest.main()
