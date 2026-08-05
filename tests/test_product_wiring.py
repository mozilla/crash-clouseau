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

from crashclouseau import app, bugzilla_apply, config, html, report_bug  # noqa: E402


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
            # Resolved once in the worker (orchestrator._resolve_candidate_git_commit) so the
            # bug comment can link the changeset on GitHub without a render-time hg lookup.
            "git_commit": "g1ta1b2c3d4",
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
                mock.patch.object(bugzilla_apply, "net") as req:
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

    def _get(self, evidence, pc=("Core", "DOM: Core & HTML"), nick="stransky"):
        # The bug-preview product::component + Bugzilla-nick lookups are networked; mock
        # Everything networked in the bug preview is mocked so the panel renders offline and
        # deterministically: product::component, the Bugzilla nick, Socorro's crash
        # reason/volume and the version lookup. The git sha comes from the candidate. The comment ITSELF is
        # composed for real from _stack() + the evidence.
        # authors_for -> {} makes the needinfo email come from the candidate author display.
        with mock.patch("crashclouseau.models.CrashStack.get_by_uuid",
                        return_value=(_stack(), _uuid_info())), \
                mock.patch.object(bugzilla_apply, "build_evidence", return_value=evidence), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.UUID.get_info",
                           return_value={"version": "155.0a1"}), \
                mock.patch.object(report_bug, "_bugzilla_nick", return_value=nick), \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"reason": "SIGSEGV", "address": "0x10"}), \
                mock.patch.object(report_bug, "fetch_signature_stats",
                                  return_value=(True, {"count": 3, "installs": 2})), \
                mock.patch.object(report_bug, "resolve_product_component", return_value=pc):
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
        # the candidate changeset node now links to the channel's hg repo
        self.assertIn("/rev?node=culpritnode1", html)
        # culprit + call path + data flow + skeptic
        self.assertIn("bug 99999", html)
        self.assertIn("A::run", html)
        self.assertIn("use-after-free of mPtr", html)
        self.assertIn("skeptic-fail", html)
        # the "Recorded Bugzilla actions" apply UI is fully removed (informative-only now)
        self.assertNotIn("Recorded Bugzilla actions", html)
        self.assertNotIn("applyActionsBtn", html)
        self.assertNotIn('class="apply-cb"', html)
        # the "Draft a bug" button is gone (superseded by the informative preview)
        self.assertNotIn("Draft a bug", html)
        self.assertNotIn("draftBug", html)
        # bug preview: product::component, Socorro-format title, and ONE comment carrying
        # the stack, the analysis, the code references and the needinfo ask
        self.assertIn("Bug we", html)
        self.assertIn("Crash in [@ Foo::bar]", html)
        self.assertIn("Core :: DOM: Core &amp; HTML", html)
        self.assertIn("Top 1 frames:", html)
        self.assertIn("Crash Reason:", html)
        self.assertIn("There are 3 crashes (from 2 installations) in nightly 155", html)
        self.assertIn("Clouseau analysis", html)
        self.assertIn("Code references:", html)
        # the culprit changeset carries an hg AND a github link
        self.assertIn(
            "Suspected regressor: "
            "[culpritnode1](https://hg.mozilla.org/mozilla-central/rev/culpritnode1) "
            "([gh](https://github.com/mozilla-firefox/firefox/commit/g1ta1b2c3d4))",
            html)
        # needinfo targets the REGRESSOR author by their BUGZILLA nick (mocked to stransky)
        self.assertIn(":stransky, can you have a look please?", html)
        # ...and the flag's requestee is surfaced as an address, not just a nick
        self.assertIn("Needinfo flag:", html)
        self.assertIn("dev@moz.example", html)
        # only one comment block is offered now (no separate Description/Comment pair)
        self.assertNotIn("<strong>Description:</strong>", html)
        self.assertEqual(html.count('<pre class="action-body">'), 1)

    def test_refutation_reason_is_hoisted_next_to_the_chip(self):
        # The chip says a dispute happened; the WHY used to live only in the second-opinion
        # section far down the page. Hoist it under the badge, next to the verdict it argues
        # against -- and still show it in full below.
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        ev["dossier"]["corroborations"] = {"second_opinion_refuted": True}
        ev["dossier"]["second_opinion"] = {
            "mode": "verify", "corroborates": False, "confidence": "high",
            "mechanism": "poisonCode memsets a stale range",
            "refutation": "The signature's first-seen build is 20260311050622 but the "
                          "changeset landed 2026-07-15 - the crash predates the change.",
        }
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("independent review disputes this", html)
        self.assertIn("Why the review disputes it:", html)
        # hoisted ABOVE the full second-opinion section, and the reason appears in both
        hoisted = html.index("Why the review disputes it:")
        section = html.index("Independent second opinion")
        self.assertLess(hoisted, section)
        self.assertEqual(html.count("the crash predates the change."), 2)

    def test_no_refutation_paragraph_when_corroborated(self):
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        ev["dossier"]["corroborations"] = {"second_opinion_corroborated": True}
        ev["dossier"]["second_opinion"] = {
            "mode": "verify", "corroborates": True, "confidence": "high",
            "mechanism": "agrees", "refutation": None,
        }
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("independently confirmed", html)
        self.assertNotIn("Why the review disputes it:", html)

    def test_recorded_actions_ui_removed(self):
        # The whole "Recorded Bugzilla actions" apply UI is removed (informative-only
        # phase); a lead with recorded actions shows the preview, not an apply trail.
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        html = self._get(ev).get_data(as_text=True)
        self.assertNotIn("Recorded Bugzilla actions", html)
        self.assertNotIn('class="apply-cb"', html)
        self.assertNotIn("applyActionsBtn", html)
        self.assertIn("Bug we", html)   # replaced by the informative preview

    def test_panel_tolerates_null_action_entry(self):
        # A null/dropped entry in payload["actions"] (schema drift) must not 500 the whole
        # page; the needinfo-draft de-dup loop still iterates actions, so it must be guarded.
        ev = _evidence()
        ev["actions"] = [None] + _actions()
        ev["apply_indices"] = bugzilla_apply.applicable_indices(ev["actions"], ev["ui"])
        rv = self._get(ev)
        self.assertEqual(rv.status_code, 200)
        self.assertIn("evidence-panel", rv.get_data(as_text=True))

    def test_mechanism_lead_without_candidate(self):
        # A lead with no pinned changeset (candidate=None) is a "mechanism lead"; the
        # blurb must NOT claim a changeset was found, and no changeset section renders.
        # Regression: the static blurb always said "a related changeset was found".
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        ev["dossier"]["candidate"] = None
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("Mechanism lead", html)
        self.assertIn("no specific regressing changeset", html)
        self.assertNotIn("changeset was found", html)
        self.assertNotIn("Possibly-related changeset", html)

    def test_lead_with_candidate_says_changeset_found(self):
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("possibly-related changeset was found", html)
        self.assertIn("Possibly-related changeset", html)
        self.assertNotIn("Mechanism lead", html)

    def test_abstain_does_not_head_its_candidate_suspected_regressor(self):
        # The suppression gates write a NEW abstain Verdict but deliberately keep
        # mechanism/consistency on it ("so the page can still explain what was found and why
        # it was dropped"), and the candidate stays on the dossier — so all three of these
        # sections RENDER on an abstain. Keyed on `is_lead` alone they fell through to the
        # CULPRIT copy, so the very page that correctly suppressed a revert still headed it
        # "Suspected regressor" and asserted its "Mechanism"/"Consistency with the crash".
        ev = _evidence(verdict="abstain", confidence=25, show_abstain=True)
        ev["dossier"]["corroborations"] = {
            "candidate_is_backout": True,
            "candidate_backout_same_push": "507de5c66b0d",
            "candidate_backout_suppressed": True,
        }
        ev["dossier"]["verdict"] = {
            "decision": "abstain",
            "confidence": "low",
            "abstain_reason": "candidate is a BACKOUT of a patch from its own push",
            "mechanism": {"statement": "use-after-free of mPtr", "citations": [_SEARCHFOX]},
            "consistency": {"statement": "matches the crashing frame", "citations": [_FRAME]},
        }
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("ABSTAIN", html)
        self.assertIn("Changeset examined", html)
        self.assertNotIn("Suspected regressor", html)
        self.assertNotIn("<h3>Mechanism</h3>", html)
        self.assertNotIn("Consistency with the crash", html)
        # The hedged lead voice is the right one for an abstain: it reports what the analysis
        # thought without asserting it. The claims themselves are still shown.
        self.assertIn("Working hypothesis (unverified)", html)
        self.assertIn("Why it may be related", html)
        self.assertIn("use-after-free of mPtr", html)

    def test_ref_citation_renders_and_survives_untyped_hunk_lines(self):
        # `DiffHunk.lines` is deliberately an UNTYPED list (the model emits bare strings
        # there), and the `cite()` macro is fed it directly — so a model-supplied int or
        # dict reaches the `ref` branch unvalidated, where `[:12]`/`.startswith` would 500
        # the whole page. Also pins that a `javascript:` permalink never becomes an href.
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        ev["dossier"]["hunks"][0]["citations"] = [_DIFF]
        ev["dossier"]["hunks"][0]["lines"] = [
            {"kind": "ref", "node": 1234567890123},
            {"kind": "ref", "node": {"a": 1}},
            {"kind": "ref", "permalink": 5},
            {"kind": "ref"},
            {"kind": "ref", "permalink": "javascript:alert(1)", "symbol_id": "A::b"},
        ]
        ev["dossier"]["verdict"]["consistency"]["citations"] = [
            {"kind": "ref", "node": "0123456789ab", "filename": "dom/Foo.cpp", "line": 4},
        ]
        rv = self._get(ev)
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("dom/Foo.cpp:4 @ 0123456789ab", html)
        self.assertNotIn("javascript:alert(1)", html)

    def test_culprit_keeps_the_assertive_headings(self):
        # The other side of the inversion above: only a culprit verdict may use the
        # assertive copy, and it must still get it.
        html = self._get(_evidence()).get_data(as_text=True)   # culprit @ 90
        self.assertIn("Suspected regressor", html)
        self.assertIn("<h3>Mechanism</h3>", html)
        self.assertIn("Consistency with the crash", html)
        self.assertNotIn("Changeset examined", html)

    def test_worth_investigating_shown_for_culprit(self):
        # Phase-2: a calibrated p_worth_investigating on the dossier verdict is surfaced as
        # the person-level "worth investigating" %, REPLACING the raw (miscalibrated) rung %.
        ev = _evidence()  # culprit, confidence=90
        ev["dossier"]["verdict"]["p_worth_investigating"] = 0.9714
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("97% worth investigating", html)
        self.assertNotIn("90%", html)   # raw rung replaced by the calibrated number

    def test_worth_investigating_shown_for_lead(self):
        ev = _evidence(verdict="lead", confidence=50)
        ev["ui"]["lead_label"] = "LEAD"
        ev["dossier"]["verdict"]["p_worth_investigating"] = 0.8
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("80% worth investigating", html)

    def test_worth_investigating_absent_falls_back_to_rung(self):
        # No calibration table wired (or an older dossier) -> p_worth None -> the panel
        # still shows the raw rung %, so nothing regresses before the table is deployed.
        ev = _evidence()
        ev["dossier"]["verdict"].pop("p_worth_investigating", None)
        html = self._get(ev).get_data(as_text=True)
        self.assertIn("90%", html)
        self.assertNotIn("worth investigating", html)

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
        with mock.patch.object(bugzilla_apply, "net") as req, \
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
                mock.patch.object(bugzilla_apply, "net") as req, \
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
        with mock.patch.object(bugzilla_apply, "net") as req, \
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

    def test_can_apply_true_for_lead_at_threshold(self):
        # #15 phase 4: a lead is apply-eligible at/above the lower lead threshold (50).
        self.assertTrue(self._build(verdict="lead", confidence=50)["can_apply"])

    def test_can_apply_false_for_lead_below_threshold(self):
        self.assertFalse(self._build(verdict="lead", confidence=40)["can_apply"])

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
        # full-diff fetch unavailable here -> falls back to the dossier's cited lines.
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=_evidence()), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff", return_value=None):
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

    def test_codeview_does_not_pin_to_build_rev(self):
        # searchfox indexes ~tip, not arbitrary build revs, so a rev= must NOT produce a
        # /rev/<build-rev>/ URL (that 500s with "Bad revision"); always use /source/.
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=None):
            rv = self.client.get(
                "/codeview.html?uuid=x&filename=a/b.cpp&rev=cf7befa1fd39&channel=nightly"
            )
        html_text = rv.get_data(as_text=True)
        self.assertIn("https://searchfox.org/firefox-main/source/a/b.cpp", html_text)
        self.assertNotIn("/rev/cf7befa1fd39", html_text)

    def test_codeview_uses_channel_tree_at_tip_not_build_rev(self):
        # beta channel -> firefox-beta tree; the build rev is NOT pinned (searchfox
        # only indexes ~tip, so /rev/<buildrev>/ 500s "Bad revision") -> /source/ (tip).
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=_evidence()), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff", return_value=None):
            rv = self.client.get(
                "/codeview.html?uuid=u-1&filename=dom/Foo.cpp&node=culpritnode1"
                "&line=42&channel=beta&rev=d0d4ceee5e1c"
            )
        self.assertEqual(rv.status_code, 200)
        t = rv.get_data(as_text=True)
        self.assertIn(
            'src="https://searchfox.org/firefox-beta/source/dom/Foo.cpp#line-42"', t,
        )
        self.assertIn(
            'href="https://searchfox.org/firefox-beta/source/dom/Foo.cpp#line-42" target="sf"',
            t,
        )
        self.assertNotIn("/rev/d0d4ceee5e1c", t)   # never pins to the build rev

    _SAMPLE_DIFF = (
        "diff --git a/dom/Foo.cpp b/dom/Foo.cpp\n"
        "--- a/dom/Foo.cpp\n"
        "+++ b/dom/Foo.cpp\n"
        "@@ -40,4 +40,5 @@ void Foo::bar() {\n"
        " context line A\n"
        "-old line\n"
        "+new line one\n"
        "+delete mPtr;\n"
        " context line B\n"
        "diff --git a/other/File.cpp b/other/File.cpp\n"
        "--- a/other/File.cpp\n"
        "+++ b/other/File.cpp\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )

    def test_file_diff_lines_parses_and_highlights(self):
        from crashclouseau import html
        cited = {("added", 42)}  # the dossier flagged the added line 42 as crash-relevant
        out = html._file_diff_lines(self._SAMPLE_DIFF, "dom/Foo.cpp", cited)
        # only the target file, not other/File.cpp
        self.assertNotIn("y", [d["content"] for d in out])
        self.assertTrue(any(d["kind"] == "hunk" for d in out))
        self.assertTrue(any(d["kind"] == "deleted" and d["content"] == "old line" for d in out))
        self.assertTrue(any(d["kind"] == "context" and d["content"] == "context line A" for d in out))
        added = [d for d in out if d["kind"] == "added"]
        self.assertEqual([d["content"] for d in added], ["new line one", "delete mPtr;"])
        self.assertEqual([d["ln"] for d in added], [41, 42])
        hl = [d for d in out if d["hl"]]
        self.assertEqual(len(hl), 1)                    # only the cited line
        self.assertEqual((hl[0]["content"], hl[0]["ln"]), ("delete mPtr;", 42))
        # two-column (old|new) numbers: added lines have no OLD number, deleted lines
        # have no NEW number, context has both (so the gutters never collide).
        self.assertEqual([(d["old"], d["new"]) for d in added], [(None, 41), (None, 42)])
        deleted = [d for d in out if d["kind"] == "deleted"]
        self.assertEqual([(d["old"], d["new"]) for d in deleted], [(41, None)])
        ctxA = next(d for d in out if d["content"] == "context line A")
        self.assertEqual((ctxA["old"], ctxA["new"]), (40, 40))

    def test_file_diff_lines_no_column_collision(self):
        # Reproduces the GtkCompositorWidget case (2 deleted -> 1 added): the single
        # column used to read 147,148,147,148 (old# of deletions colliding with new# of
        # the addition + following context). With split gutters, deletions carry only an
        # OLD number and the addition/context only a NEW number — no collision.
        from crashclouseau import html
        diff = (
            "diff --git a/w/G.cpp b/w/G.cpp\n--- a/w/G.cpp\n+++ b/w/G.cpp\n"
            "@@ -145,6 +145,5 @@ foo() {\n"
            " ctx145\n ctx146\n"
            "-  if (a && b &&\n-      c) {\n"
            "+  if (a) {\n"
            " ctx148\n"
        )
        out = html._file_diff_lines(diff, "w/G.cpp", set())
        dels = [(d["old"], d["new"]) for d in out if d["kind"] == "deleted"]
        adds = [(d["old"], d["new"]) for d in out if d["kind"] == "added"]
        self.assertEqual(dels, [(147, None), (148, None)])   # old-file numbers only
        self.assertEqual(adds, [(None, 147)])                # new-file number only
        tail = [d for d in out if d["content"] == "ctx148"][0]
        self.assertEqual((tail["old"], tail["new"]), (149, 148))  # new# monotonic: ...147(add),148(ctx)

    def test_codeview_full_file_diff_with_highlight(self):
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=_evidence()), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff",
                        return_value=self._SAMPLE_DIFF):
            rv = self.client.get(
                "/codeview.html?uuid=u-1&filename=dom/Foo.cpp&node=culpritnode1"
                "&line=42&channel=nightly"
            )
        t = rv.get_data(as_text=True)
        self.assertIn("new line one", t)   # full-file added line (not crash-relevant)
        self.assertIn("dl-deleted", t)     # full diff shows deletions
        self.assertIn("dl-context", t)     # ...and context
        self.assertIn("dl-hunk", t)        # ...and hunk headers
        self.assertIn("dl-added hl", t)    # the cited line is highlighted
        self.assertNotIn("other/File.cpp", t)  # scoped to the one file

    def test_codeview_full_diff_degrades_to_cited_lines(self):
        # If the changeset diff can't be fetched, fall back to the dossier's cited lines.
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=_evidence()), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff", return_value=None):
            rv = self.client.get(
                "/codeview.html?uuid=u-1&filename=dom/Foo.cpp&node=culpritnode1"
                "&line=42&channel=nightly"
            )
        t = rv.get_data(as_text=True)
        self.assertIn("delete mPtr;", t)   # cited line still shown
        self.assertNotIn("dl-hunk", t)     # but no full-diff hunk headers

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


class TestUiEnvOverride(unittest.TestCase):
    """SHOW_ABSTAIN env override lets a canary surface every triaged crash's panel
    (incl. abstains) for evaluation, without touching the shared config."""

    def test_show_abstain_env_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHOW_ABSTAIN", None)
            self.assertFalse(config.get_agent_ui()["show_abstain"])  # default off
            os.environ["SHOW_ABSTAIN"] = "1"
            self.assertTrue(config.get_agent_ui()["show_abstain"])
            os.environ["SHOW_ABSTAIN"] = "false"
            self.assertFalse(config.get_agent_ui()["show_abstain"])


class TestLinkify(unittest.TestCase):
    """The panel's free-text fields hyperlink bug/changeset refs, safely (escape first)."""

    def test_bug_and_changeset_and_escaping(self):
        from crashclouseau import linkify
        out = str(linkify("bug 1867743 in fdb65c5972a9 <script>x</script>",
                          "https://hg.mozilla.org/mozilla-central"))
        self.assertIn('href="https://bugzilla.mozilla.org/1867743"', out)
        self.assertIn('/rev?node=fdb65c5972a9"', out)
        self.assertIn("&lt;script&gt;", out)      # HTML escaped, not injected
        self.assertNotIn("<script>", out)

    def test_no_repo_url_leaves_hashes_plain(self):
        from crashclouseau import linkify
        out = str(linkify("bug 42 and abcdef123456", ""))
        self.assertIn("bugzilla.mozilla.org/42", out)
        self.assertNotIn("/rev?node=", out)

    def test_empty(self):
        from crashclouseau import linkify
        self.assertEqual(linkify(None), "")

    def test_backtick_code_becomes_code_tag(self):
        from crashclouseau import linkify
        out = str(linkify("guarded by `ASSERT(textureUnit != -1)` only", ""))
        self.assertIn("<code>ASSERT(textureUnit != -1)</code>", out)

    def test_code_span_escaped_and_exempt_from_rewrites(self):
        from crashclouseau import linkify
        # C++ inside a code span stays literal: -> not turned into an arrow, <T> escaped,
        # and a hash inside code is not link-ified.
        out = str(linkify("see `mHead -> next` and `Foo<T>` and `abcdef123456`",
                          "https://hg.mozilla.org/mozilla-central"))
        self.assertIn("<code>mHead -&gt; next</code>", out)   # literal ->, no arrow
        self.assertNotIn("→", out)
        self.assertIn("<code>Foo&lt;T&gt;</code>", out)
        self.assertIn("<code>abcdef123456</code>", out)       # not a rev link
        self.assertNotIn("/rev?node=abcdef123456", out)

    def test_prose_around_code_still_linkified(self):
        from crashclouseau import linkify
        out = str(linkify("`x` from bug 42 in fdb65c5972a9",
                          "https://hg.mozilla.org/mozilla-central"))
        self.assertIn("<code>x</code>", out)
        self.assertIn("bugzilla.mozilla.org/42", out)
        self.assertIn("/rev?node=fdb65c5972a9", out)

    def test_spaced_arrows_prettified(self):
        from crashclouseau import linkify
        out = str(linkify("A::f -> B::g <- C::h <-> D::i", ""))
        self.assertIn("A::f → B::g ← C::h ↔ D::i", out)
        self.assertNotIn("->", out)
        self.assertNotIn("<-", out)

    def test_cpp_member_access_not_touched(self):
        from crashclouseau import linkify
        out = str(linkify("HidePopover dereferences data->SetInvoker(nullptr)", ""))
        self.assertIn("data-&gt;SetInvoker", out)   # C++ arrow escaped, not converted
        self.assertNotIn("→", out)

    def test_expert_reason_links_hash_and_bug(self):
        from crashclouseau import linkify
        out = str(linkify("authored candidate 1404f5a62772 (bug 2014622)",
                          "https://hg.mozilla.org/mozilla-central"))
        self.assertIn("/rev?node=1404f5a62772", out)
        self.assertIn("bugzilla.mozilla.org/2014622", out)


class TestReportsIndexBadges(unittest.TestCase):
    """The reports index badges culprit/lead always; abstain only in show_abstain mode."""

    def _render(self, verdict, show_abstain):
        from flask import render_template
        sigs = [("Sig::A", {"uuids": [("uuid-x", 5)], "number": 1, "installs": 1, "url": "#"})]
        with app.test_request_context():
            return render_template(
                "reports.html", buildids="{}",
                products={"Firefox": {"nightly": [["20260101000000", "1.0"]]}},
                selected_product="Firefox", selected_channel="nightly",
                selected_bid="20260101000000", signatures=sigs,
                verdicts={"uuid-x": {"verdict": verdict, "confidence": 20}},
                show_abstain=show_abstain, colors={5: "#eee"},
            )

    def test_abstain_badge_only_when_show_abstain(self):
        self.assertIn(">abstain</span>", self._render("abstain", True))
        self.assertNotIn(">abstain</span>", self._render("abstain", False))

    def test_lead_and_culprit_always_badged(self):
        self.assertIn(">lead</span>", self._render("lead", False))
        self.assertIn(">culprit</span>", self._render("culprit", False))


class TestBugPreview(unittest.TestCase):
    """report_bug.build_bug_preview & helpers: recreate the Socorro crash comment locally
    and resolve the target product::component from the regressor (fallback = the author's
    recent patches' most frequent P::C)."""

    def _stack3(self):
        return {"frames": [
            {"stackpos": 0, "function": "Foo::bar", "filename": "dom/Foo.cpp", "line": 51,
             "module": "xul.dll"},
            {"stackpos": 1, "function": "os_unfair_lock", "filename": "", "line": 0,
             "module": "libsystem_platform.dylib",
             "original": "os_unfair_lock (in libsystem_platform.dylib)"},
            {"stackpos": 2, "function": "Baz::qux", "filename": "gfx/Baz.cpp", "line": -1,
             "module": "xul.dll"},
        ]}

    def test_build_frames_block_format(self):
        c = report_bug.build_frames_block(self._stack3())
        self.assertIn("Top 3 frames:", c)
        # Fenced, because BMO renders comments as markdown and would mangle C++ otherwise.
        self.assertIn("```", c)
        # Socorro's column order: stackpos, module, function, file:line.
        self.assertIn("0  xul.dll  Foo::bar  dom/Foo.cpp:51", c)
        self.assertIn("1  libsystem_platform.dylib  os_unfair_lock", c)  # no file
        self.assertIn("2  xul.dll  Baz::qux  gfx/Baz.cpp", c)  # line -1 dropped, file kept

    def test_build_frames_block_caps_frames(self):
        c = report_bug.build_frames_block(self._stack3(), max_frames=2)
        self.assertIn("Top 2 frames:", c)
        self.assertIn("1  libsystem_platform.dylib  os_unfair_lock", c)
        self.assertNotIn("Baz::qux", c)

    def test_build_frames_block_truncates_long_function(self):
        # A heavily-templated signature is cut at Socorro's width so one frame stays one
        # line; the file:line still follows it.
        fn = "mozilla::Foo<" + "T" * 200 + ">::bar()"
        stack = {"frames": [{"stackpos": 0, "function": fn, "filename": "a.cpp",
                             "line": 3, "module": "xul.dll"}]}
        c = report_bug.build_frames_block(stack)
        self.assertIn("...", c)
        self.assertIn("a.cpp:3", c)
        self.assertNotIn("T" * 100, c)

    def test_build_frames_block_falls_back_to_original(self):
        # No symbols at all -> Socorro's raw frame text, rather than an empty line.
        stack = {"frames": [{"stackpos": 0, "function": "", "filename": "", "line": 0,
                             "module": "", "original": "0x7ffd (in unknown)"}]}
        self.assertIn("0  0x7ffd (in unknown)", report_bug.build_frames_block(stack))

    def test_build_reason_block_moz_crash(self):
        b = report_bug.build_reason_block({"moz_crash_reason": "not implemented: no wipe",
                                           "reason": "SIGSEGV", "address": "0x0"})
        # A panic/MOZ_CRASH gets the hand-filed heading and its own text wins over the
        # OS-level reason.
        self.assertTrue(b.startswith("MOZ_CRASH Reason:"))
        self.assertIn("not implemented: no wipe", b)
        self.assertNotIn("SIGSEGV", b)

    def test_build_reason_block_os_reason_with_address(self):
        b = report_bug.build_reason_block(
            {"reason": "EXCEPTION_ACCESS_VIOLATION_READ", "address": "0x0000000000000000"})
        self.assertTrue(b.startswith("Crash Reason:"))
        self.assertIn("EXCEPTION_ACCESS_VIOLATION_READ at 0x0000000000000000", b)

    def test_build_reason_block_none_when_empty(self):
        self.assertIsNone(report_bug.build_reason_block(None))
        self.assertIsNone(report_bug.build_reason_block({}))
        self.assertIsNone(report_bug.build_reason_block({"reason": "", "address": "0x0"}))

    def test_build_stats_sentence(self):
        info = {"channel": "nightly", "version": "155.0a1",
                "buildid": "20260727081724"}
        s = report_bug.build_stats_sentence(True, {"count": 2, "installs": 2}, info)
        self.assertEqual(
            s, "There are 2 crashes (from 2 installations) in nightly 155 "
               "with buildid 20260727081724.")
        # not the only buildid -> "starting with"
        s = report_bug.build_stats_sentence(False, {"count": 2, "installs": 2}, info)
        self.assertIn("starting with buildid 20260727081724.", s)
        # singulars
        self.assertIn("There is 1 crash", report_bug.build_stats_sentence(
            True, {"count": 1, "installs": 1}, info))
        self.assertIn("from 1 installation)", report_bug.build_stats_sentence(
            True, {"count": 4, "installs": 1}, info))
        # a release channel is named by its full version, not channel + major
        self.assertIn(" in 154.0.1 with", report_bug.build_stats_sentence(
            True, {"count": 2, "installs": 2},
            {"channel": "release", "version": "154.0.1", "buildid": "2026"}))

    def test_build_stats_sentence_none_without_counts(self):
        self.assertIsNone(report_bug.build_stats_sentence(True, None, {}))
        self.assertIsNone(report_bug.build_stats_sentence(True, {"count": 0}, {}))

    def test_build_code_references(self):
        verdict = {
            "mechanism": {"citations": [
                {"kind": "searchfox", "symbol_id": "A::b",
                 "permalink": "https://searchfox.org/x#1"},
                {"kind": "diff_line", "filename": "a/B.cpp", "line": 9, "node": "abc"},
            ]},
            "consistency": {"citations": [
                # duplicate URL -> emitted once
                {"kind": "searchfox", "symbol_id": "A::b",
                 "permalink": "https://searchfox.org/x#1"},
                # a stack_frame cite is already in the stack block -> not a link
                {"kind": "stack_frame", "filename": "c.cpp", "line": 1, "stackpos": 0},
            ]},
        }
        refs = report_bug.build_code_references(verdict, "nightly")
        self.assertIn("- [A::b](https://searchfox.org/x#1)", refs)
        self.assertIn("- [a/B.cpp:9](https://hg.mozilla.org/mozilla-central/file/abc/"
                      "a/B.cpp#l9)", refs)
        self.assertEqual(refs.count("searchfox.org/x#1"), 1)
        self.assertNotIn("c.cpp", refs)

    def test_build_code_references_renders_a_ref_citation(self):
        # `ref` is the catch-all kind for what the source/history tools read. Without a
        # branch here it was silently absent from the filed bug — the same "the evidence
        # exists but the report doesn't say so" failure the kind was added to stop.
        verdict = {"mechanism": {"citations": [
            {"kind": "ref", "node": "abc", "filename": "a/B.cpp", "line": 9},
            {"kind": "ref", "node": "def456789012"},          # changeset, no file
            {"kind": "ref", "permalink": "https://example.org/x", "symbol_id": "A::b"},
            {"kind": "ref", "content": "-  delete mFoo;"},    # nothing linkable -> skipped
        ]}}
        refs = report_bug.build_code_references(verdict, "nightly")
        self.assertIn("- [a/B.cpp:9](https://hg.mozilla.org/mozilla-central/file/abc/"
                      "a/B.cpp#l9)", refs)
        self.assertIn("- [def456789012](https://hg.mozilla.org/mozilla-central/rev/"
                      "def456789012)", refs)
        self.assertIn("- [A::b](https://example.org/x)", refs)
        self.assertNotIn("delete mFoo", refs)

    def test_ref_with_a_label_not_a_path_links_to_the_changeset(self):
        # `ref` is the catch-all kind, so the model sometimes puts a LABEL where a repo path
        # belongs (prod dossier 4986: "hg-changeset-metadata"). /file/<node>/<label> is a 404
        # in a list whose whole purpose is letting a human check the analysis, so it must
        # fall through to /rev/<node>, which exists.
        refs = report_bug.build_code_references(
            {"consistency": {"citations": [
                {"kind": "ref", "node": "ff789e9f149e", "filename": "hg-changeset-metadata"},
            ]}}, "nightly")
        self.assertIn("- [ff789e9f149e](https://hg.mozilla.org/mozilla-central/rev/"
                      "ff789e9f149e)", refs)
        self.assertNotIn("hg-changeset-metadata", refs)

    def test_build_code_references_caps_and_empties(self):
        self.assertIsNone(report_bug.build_code_references(None, "nightly"))
        self.assertIsNone(report_bug.build_code_references(
            {"mechanism": {"citations": [{"kind": "stack_frame"}]}}, "nightly"))

        def cites(n, off=0):
            return [{"kind": "searchfox", "symbol_id": "s%d" % i,
                     "permalink": "https://searchfox.org/x#%d" % i}
                    for i in range(off, off + n)]

        cap = report_bug._MAX_CODE_REFS
        self.assertEqual(
            report_bug.build_code_references(
                {"mechanism": {"citations": cites(20)}}, "nightly").count("\n- "), cap)
        # The cap holds across BOTH claims, not per-claim: mechanism alone already fills it.
        both = {"mechanism": {"citations": cites(cap)},
                "consistency": {"citations": cites(5, off=cap)}}
        self.assertEqual(
            report_bug.build_code_references(both, "nightly").count("\n- "), cap)

    def test_changeset_links_both_forges(self):
        s = report_bug.changeset_links("74675cc139d9", "nightly", "9d7faea5127c")
        self.assertEqual(
            s,
            "[74675cc139d9](https://hg.mozilla.org/mozilla-central/rev/74675cc139d9) "
            "([gh](https://github.com/mozilla-firefox/firefox/commit/9d7faea5127c))")

    def test_changeset_links_drops_gh_when_unmapped(self):
        # No git counterpart -> hg only, never a dead github link.
        s = report_bug.changeset_links("abc123", "nightly")
        self.assertEqual(s, "[abc123](https://hg.mozilla.org/mozilla-central/rev/abc123)")

    def test_changeset_links_bare_node_without_channel(self):
        # No channel -> no repo to link into; the node still gets named.
        self.assertEqual(report_bug.changeset_links("abc123", None, "g1t"), "abc123")
        self.assertEqual(report_bug.changeset_links("", "nightly", "g1t"), "")

    def test_changeset_links_never_makes_a_request(self):
        # hg's json-rev costs 8-13s; resolving it here made a cold crashstack render take 15s.
        # The sha is resolved once in the worker and passed in, so rendering must not touch hg.
        with mock.patch.object(report_bug.net, "get", side_effect=AssertionError("no hg!")):
            s = report_bug.changeset_links("74675cc139d9", "nightly", "9d7faea5127c")
        self.assertIn("/commit/9d7faea5127c", s)

    def test_build_bug_comment_section_order(self):
        # ONE comment, in the order a triager reads a hand-filed crash bug.
        dossier = {
            "candidate": {"node": "abc123", "bug": 7, "author": "Dev",
                          "git_commit": "g1t"},
            "verdict": {"confidence": "probable",
                        "mechanism": {"statement": "null deref of mFoo", "citations": [
                            {"kind": "searchfox", "symbol_id": "A::b",
                             "permalink": "https://searchfox.org/x#1"}]}},
        }
        info = {"uuid": "u-1", "channel": "nightly", "buildid": "20260727081724"}
        c = report_bug.build_bug_comment(
            info, self._stack3(), dossier,
            details={"reason": "SIGSEGV", "address": "0x0"},
            stats={"count": 2, "installs": 2}, first=True, version="155.0a1",
            needinfo=":dev, can you have a look please?")
        order = [
            "Crash report: https://crash-stats.mozilla.org/report/index/u-1",
            "Crash Reason:",
            "Top 3 frames:",
            "There are 2 crashes",
            "Clouseau analysis (confidence probable): null deref of mFoo",
            "Suspected regressor: [abc123](", "([gh](", ") (bug 7) by Dev.",
            "Code references:",
            ":dev, can you have a look please?",
        ]
        at = [c.find(x) for x in order]
        self.assertNotIn(-1, at, "missing section in {!r}".format(c))
        self.assertEqual(at, sorted(at), "sections out of order")

    def test_build_bug_comment_drops_empty_sections(self):
        # No reason, no stats, no citations, no needinfo -> no empty headings, no blank runs.
        info = {"uuid": "u-1", "channel": "nightly", "buildid": "1"}
        c = report_bug.build_bug_comment(
            info, self._stack3(), {"candidate": {"node": "n"}, "verdict": {}})
        self.assertNotIn("Reason:", c)
        self.assertNotIn("Code references:", c)
        self.assertNotIn("There are", c)
        self.assertNotIn("\n\n\n", c)

    def test_resolve_pc_uses_regressor_bug(self):
        with mock.patch.object(report_bug, "_bugs_product_component",
                               return_value={123: ("Core", "DOM")}):
            pc = report_bug.resolve_product_component({"bug": 123, "node": "n"}, "nightly")
        self.assertEqual(pc, ("Core", "DOM"))

    def test_resolve_pc_falls_back_to_author_patches(self):
        # Regressor bug unreadable (security) -> most frequent P::C over the author's
        # recent patches' bugs.
        def fake_pc(bugids):
            bugids = list(bugids)
            if bugids == [123]:
                return {}                       # regressor bug unreadable
            return {1: ("Core", "X"), 2: ("Core", "X"), 3: ("Toolkit", "Y")}
        with mock.patch.object(report_bug, "_bugs_product_component", side_effect=fake_pc), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author",
                           return_value=[1, 2, 3]):
            pc = report_bug.resolve_product_component(
                {"bug": 123, "node": "n", "author": "Dev <dev@x.com>"}, "nightly")
        self.assertEqual(pc, ("Core", "X"))     # most frequent

    def test_resolve_pc_fallback_tie_prefers_newest(self):
        # A count tie in the author-patches fallback must deterministically pick the
        # NEWEST patch's P::C, independent of _bugs_product_component's (cache-driven)
        # dict order. Here _bugs_product_component returns older-bug-first (as it would if
        # bug 100 were pre-cached), yet the newest bug (150) must win.
        def fake_pc(bugids):
            bugids = list(bugids)
            if bugids == [123]:
                return {}                                  # regressor unreadable
            return {100: ("Core", "DOM"), 150: ("Toolkit", "Y")}  # older-first order
        with mock.patch.object(report_bug, "_bugs_product_component", side_effect=fake_pc), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author",
                           return_value=[150, 100]):        # newest-first
            pc = report_bug.resolve_product_component(
                {"bug": 123, "node": "n", "author": "Dev <dev@x.com>"}, "nightly")
        self.assertEqual(pc, ("Toolkit", "Y"))             # newest wins the tie

    def test_resolve_pc_none_when_unresolvable(self):
        with mock.patch.object(report_bug, "_bugs_product_component", return_value={}), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author",
                           return_value=[]):
            pc = report_bug.resolve_product_component(
                {"bug": 123, "node": "n", "author": ""}, "nightly")
        self.assertEqual(pc, (None, None))

    def test_build_bug_preview_none_without_candidate(self):
        ui = {"uuid": "u-1", "signature": "S", "channel": "nightly"}
        self.assertIsNone(report_bug.build_bug_preview(ui, self._stack3(), None))
        self.assertIsNone(report_bug.build_bug_preview(ui, self._stack3(), {"candidate": None}))
        self.assertIsNone(
            report_bug.build_bug_preview(ui, self._stack3(), {"candidate": {"bug": 1}}))

    def test_build_bug_preview_shape(self):
        ui = {"uuid": "u-1", "signature": "Foo::bar", "channel": "nightly",
              "buildid": "20260727081724"}
        dossier = {
            "candidate": {"node": "n", "bug": 1, "author": "Dev <dev@x.com>",
                          "git_commit": "g1tsha"},
            "verdict": {"confidence": "high",
                        "mechanism": {"statement": "UAF of mFoo"},
                        "consistency": {"statement": "matches the crash"}},
        }
        # The needinfo targets the regressor author by their BUGZILLA nick, looked up from
        # the author email (here sourced from the hgauthor record of the candidate node).
        with mock.patch.object(report_bug, "resolve_product_component",
                               return_value=("Core", "DOM")), \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"reason": "SIGSEGV"}), \
                mock.patch.object(report_bug, "fetch_signature_stats",
                                  return_value=(True, {"count": 3, "installs": 2})), \
                mock.patch("crashclouseau.models.UUID.get_info",
                           return_value={"version": "155.0a1"}), \
                mock.patch("crashclouseau.models.Node.authors_for",
                           return_value={"n": {"nick": "hgnick", "real": "Dev",
                                               "email": "dev@x.com"}}), \
                mock.patch.object(report_bug, "_bugzilla_nick", return_value="bznick") as bz:
            prev = report_bug.build_bug_preview(ui, self._stack3(), dossier)
        self.assertEqual(prev["title"], "Crash in [@ Foo::bar]")
        self.assertEqual((prev["product"], prev["component"]), ("Core", "DOM"))
        # ONE comment carrying every section -- no separate description/explanation.
        self.assertNotIn("explanation", prev)
        c = prev["comment"]
        self.assertIn("Top 3 frames:", c)
        self.assertIn("Crash Reason:", c)
        self.assertIn("There are 3 crashes (from 2 installations) in nightly 155", c)
        self.assertIn("UAF of mFoo", c)
        self.assertIn("Suspected regressor: [n](", c)
        self.assertIn("/commit/g1tsha)) (bug 1)", c)
        self.assertIn(":bznick, can you have a look please?", c)
        # the Bugzilla nick wins over the hg nick, and it's looked up from the email
        self.assertEqual(prev["needinfo"], ":bznick, can you have a look please?")
        # the flag needs an address, not a display nick
        self.assertEqual(prev["needinfo_email"], "dev@x.com")
        bz.assert_called_once_with("dev@x.com")
        # Metadata a create_bug is REJECTED without, plus what a hand-filed crash bug carries.
        self.assertEqual(prev["version"], "Trunk")          # nightly
        self.assertEqual(prev["type"], "defect")
        self.assertEqual(prev["keywords"], ["crash", "regression"])
        self.assertEqual(prev["cf_crash_signature"], "[@ Foo::bar]")
        self.assertEqual(prev["blocked"], ["clouseau", 1])   # tracking bug + the regressor's
        # NEVER asserted automatically: the pipeline is not accurate enough to claim causation
        # as structured data (the blind review refutes ~74% of leads).
        self.assertNotIn("regressed_by", prev)

    def test_build_bug_preview_version_off_nightly(self):
        """`Trunk` is only right for nightly; every product also has `unspecified`, whereas a
        "Firefox NNN" value may not be active in the product we resolved."""
        self.assertEqual(report_bug._bug_version("nightly"), "Trunk")
        self.assertEqual(report_bug._bug_version("Nightly"), "Trunk")
        self.assertEqual(report_bug._bug_version("beta"), "unspecified")
        self.assertEqual(report_bug._bug_version(""), "unspecified")
        self.assertEqual(report_bug._bug_version(None), "unspecified")

    def test_build_bug_preview_blocked_without_a_regressor_bug(self):
        ui = {"uuid": "u-1", "signature": "Foo::bar", "channel": "nightly",
              "buildid": "20260727081724"}
        dossier = {"candidate": {"node": "n", "author": "Dev <dev@x.com>"},
                   "verdict": {"confidence": "medium"}}
        with mock.patch.object(report_bug, "resolve_product_component",
                               return_value=("Core", "DOM")), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}), \
                mock.patch.object(report_bug, "fetch_signature_stats",
                                  return_value=(True, {})), \
                mock.patch("crashclouseau.models.UUID.get_info", return_value={}), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch.object(report_bug, "_bugzilla_nick", return_value=None):
            prev = report_bug.build_bug_preview(ui, self._stack3(), dossier)
        self.assertEqual(prev["blocked"], ["clouseau"])

    def test_needinfo_person_uses_bugzilla_nick(self):
        # email/name from the hgauthor record, nick from the Bugzilla user API.
        with mock.patch("crashclouseau.models.Node.authors_for",
                        return_value={"n": {"nick": "hgnick", "real": "Dev", "email": "d@x"}}), \
                mock.patch.object(report_bug, "_bugzilla_nick", return_value="stransky") as bz:
            p = report_bug._needinfo_person({"node": "n", "author": "Ignored <i@x>"}, "nightly")
        self.assertEqual(p["nick"], "stransky")   # bugzilla nick, NOT the hg "hgnick"
        bz.assert_called_once_with("d@x")
        # no DB record + no bugzilla nick -> name/email from the author display string
        with mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch.object(report_bug, "_bugzilla_nick", return_value=""):
            p = report_bug._needinfo_person(
                {"node": "n", "author": "Real Name <real@x.com>"}, "nightly")
        self.assertEqual((p["nick"], p["name"], p["email"]), ("", "Real Name", "real@x.com"))

    def test_bugzilla_nick_lookup(self):
        report_bug._NICK_CACHE.clear()
        captured = {"constructions": 0}

        # Faithful to the real libmozdata API: BugzillaUser has NO get_data(); the query is
        # fired in the constructor and the handler runs when wait() drains it.
        class FakeBZUser:
            def __init__(self, user_names=None, include_fields=None,
                         user_handler=None, user_data=None, **kw):
                captured["names"] = user_names
                captured["constructions"] += 1
                self._handler, self._data = user_handler, user_data

            def wait(self):
                self._handler({"name": "stransky@x.com", "nick": "stransky"}, self._data)
                return self

        with mock.patch.object(report_bug, "BugzillaUser", FakeBZUser):
            nick = report_bug._bugzilla_nick("stransky@x.com")
            nick2 = report_bug._bugzilla_nick("stransky@x.com")   # served from cache
        self.assertEqual(nick, "stransky")
        self.assertEqual(nick2, "stransky")
        self.assertEqual(captured["names"], ["stransky@x.com"])
        self.assertEqual(captured["constructions"], 1)            # cached: only one lookup
        self.assertEqual(report_bug._bugzilla_nick(""), "")
        report_bug._NICK_CACHE.clear()

    def test_needinfo_line_prefers_nick_then_name(self):
        self.assertEqual(report_bug._needinfo_line({"nick": "foo"}),
                         ":foo, can you have a look please?")
        self.assertEqual(report_bug._needinfo_line({"nick": "", "name": "Foo Bar"}),
                         "Foo Bar, can you have a look please?")
        self.assertIsNone(report_bug._needinfo_line({}))
        self.assertIsNone(report_bug._needinfo_line({"nick": "", "name": "", "email": ""}))

    def test_explanation_comment_regressor_only(self):
        # No mechanism -> still names the suspected regressor; nothing at all -> None.
        exp = report_bug._explanation_comment({}, {"node": "abc", "bug": 7})
        self.assertIn("Suspected regressor: abc (bug 7)", exp)
        self.assertIsNone(report_bug._explanation_comment({}, {}))
