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

from crashclouseau import app, bugzilla_apply, config, html, population, report_bug  # noqa: E402


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
        # The candidate came from THIS build's pushlog window, which is what earns the filed
        # bug the words "Suspected regressor" (report_bug.is_suspected_regression).
        "corroborations": {"candidate_in_pushlog_window": True},
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

    def _get(self, evidence, pc=("Core", "DOM: Core & HTML"), nick="stransky", pop=None):
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
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value={"exists": True, "nick": nick}), \
                mock.patch.object(report_bug, "fetch_crash_reason",
                                  return_value={"reason": "SIGSEGV", "address": "0x10"}), \
                mock.patch.object(report_bug, "fetch_signature_stats",
                                  return_value=(True, {"count": 3, "installs": 2})), \
                mock.patch.object(report_bug, "resolve_product_component", return_value=pc), \
                mock.patch.object(population, "for_crash", return_value=pop):
            # population.for_crash is mocked for the same reason as everything else here: it is
            # two live SuperSearches, and the panel tests must not touch the network.
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

    def test_an_unresolvable_needinfo_account_is_stated_not_hidden(self):
        """The panel is "the bug we'd file". An hg commit address is often not a Bugzilla
        login, and BMO rejects a create whose requestee it cannot resolve — so we file with
        no flag. Rendering NOTHING there reads as "no needinfo wanted", which is the wrong
        story: we wanted one and could not find the account."""
        with mock.patch.object(report_bug, "_needinfo_account", return_value={}):
            html = self._get(_evidence()).get_data(as_text=True)
        self.assertIn("no Bugzilla account resolved", html)
        # the hg address appears elsewhere on the page (the regressor line, the comment) —
        # what must NOT happen is it being offered as the flag's requestee.
        self.assertNotIn('<span class="needinfo-draft">dev@moz.example', html)
        # the prose still names the human, so a triager can set the flag in one click
        self.assertIn("can you have a look please?", html)

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

    # --- crash population block (reports vs install_time) ---

    def _pop(self, **over):
        base = {
            "crashes": 15, "faceted_crashes": 15, "installs": 3, "dropped": 0,
            "per_install": 5.0, "top_crashes": 10, "top_share": 10 / 15,
            "median_gap_s": 86400, "own": None, "single_install": False,
            "concentrated": False, "clustered": False, "truncated": False,
            "since": "2026-08-11", "channel": "nightly", "product": "Firefox",
            "build": {"crashes": 3, "installs": 2},
        }
        base.update(over)
        return base

    def test_population_block_absent_without_data(self):
        # No population -> no block at all, and no empty frame left behind.
        self.assertNotIn("pop-block", self._get(None).get_data(as_text=True))

    def test_population_numbers_rendered(self):
        body = self._get(None, pop=self._pop()).get_data(as_text=True)
        self.assertIn("pop-block", body)
        self.assertIn("crash reports", body)
        self.assertIn("install_time values", body)
        self.assertIn("5.0", body)              # reports per install
        self.assertIn("67", body)               # busiest install's share, rounded
        self.assertIn("1.0d", body)             # median gap through the human_gap filter
        self.assertIn("2026-08-11", body)       # the window is stated, not implied
        self.assertIn("3<small> / 2</small>", body)  # this build's reports/installs
        # A healthy population raises no flag chip.
        self.assertNotIn("corrob-chip refute", body)

    def test_single_install_flagged(self):
        body = self._get(None, pop=self._pop(
            crashes=1066, faceted_crashes=1066, installs=1, per_install=1066.0,
            top_crashes=1066, top_share=1.0, median_gap_s=None,
            single_install=True, concentrated=True)).get_data(as_text=True)
        self.assertIn("ONE install_time", body)
        self.assertIn("tc-warn", body)
        # The single-install chip replaces the concentration one rather than doubling up.
        self.assertNotIn("supplies 100%", body)
        # No gap card when there is only one install (no gap exists, and 0 would read as
        # "clustered" -- the exact misreading the block exists to prevent).
        self.assertNotIn("median gap between installs", body)

    def test_clustered_installs_flagged(self):
        body = self._get(None, pop=self._pop(
            installs=25, crashes=25, faceted_crashes=25, per_install=1.0,
            top_crashes=2, top_share=0.08, median_gap_s=20,
            clustered=True)).get_data(as_text=True)
        self.assertIn("not independent users", body)
        self.assertIn("20s", body)

    def test_own_install_and_caveats_rendered(self):
        body = self._get(None, pop=self._pop(
            own={"crashes": 10, "share": 10 / 15, "rank": 1, "install_time": 1},
            dropped=2, truncated=True)).get_data(as_text=True)
        self.assertIn("This report's own installation accounts for 10", body)
        self.assertIn("#1 busiest", body)
        self.assertIn("dropped as unreadable", body)
        self.assertIn("install count is a floor", body)

    def test_population_lookup_failure_does_not_break_the_page(self):
        # html.crashstack wraps the call; a raising lookup must still render the stack.
        with mock.patch.object(population, "for_crash", side_effect=RuntimeError("boom")), \
                mock.patch("crashclouseau.models.CrashStack.get_by_uuid",
                           return_value=(_stack(), _uuid_info())), \
                mock.patch.object(bugzilla_apply, "build_evidence", return_value=None):
            rv = self.client.get("/crashstack.html?uuid=u-1")
        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("pop-block", rv.get_data(as_text=True))


class TestApplyRecordedActions(unittest.TestCase):
    """The apply/replay step: bounded by enabled_types, idempotent, mocked REST."""

    def _run(self, indices, actions=None, token="TESTTOKEN",
             verdict="culprit", confidence=90):
        actions = _actions() if actions is None else actions
        ev = {"verdict": verdict, "confidence": confidence, "actions": actions}
        with mock.patch.object(bugzilla_apply, "net") as req, \
                mock.patch.object(bugzilla_apply.config, "get_bugzilla_token",
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
                mock.patch.object(bugzilla_apply.config, "get_bugzilla_token",
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
                mock.patch.object(bugzilla_apply.config, "get_bugzilla_token",
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
    # The apply route WRITES to production Bugzilla, so it is behind a shared secret
    # (`API_WRITE_TOKEN` + `X-Clouseau-Token`). Every request here carries it; the
    # unauthenticated cases are covered by TestApplyRouteAuth below.
    TOKEN = "test-write-token"

    def setUp(self):
        self.client = app.test_client()
        self._env = mock.patch.dict(os.environ, {"API_WRITE_TOKEN": self.TOKEN})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _post(self, body):
        return self.client.post("/api/evidence/apply", json=body,
                                headers={"X-Clouseau-Token": self.TOKEN})

    def test_missing_uuid_400(self):
        rv = self._post({"indices": [0]})
        self.assertEqual(rv.status_code, 400)

    def test_bad_indices_400(self):
        rv = self._post({"uuid": "u-1", "indices": "x"})
        self.assertEqual(rv.status_code, 400)

    def test_boolean_indices_rejected_400(self):
        # JSON booleans are ints in Python; they must not slip through as indices.
        rv = self._post({"uuid": "u-1", "indices": [True]})
        self.assertEqual(rv.status_code, 400)

    def test_ok(self):
        payload = [{"index": 0, "type": "bugzilla.add_comment", "ok": True, "result_id": 111}]
        with mock.patch.object(bugzilla_apply, "apply_recorded_actions",
                               return_value=payload) as ap:
            rv = self._post({"uuid": "u-1", "indices": [0]})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {"uuid": "u-1", "results": payload})
        ap.assert_called_once_with("u-1", [0])

    def test_unknown_uuid_404(self):
        with mock.patch.object(bugzilla_apply, "apply_recorded_actions",
                               side_effect=LookupError):
            rv = self._post({"uuid": "u-1", "indices": [0]})
        self.assertEqual(rv.status_code, 404)


class TestApplyRouteAuth(unittest.TestCase):
    """`/api/evidence/apply` posts comments and needinfo flags to production BMO with the
    deployment's API key, and was reachable by anyone holding a uuid — which the public
    reports pages and `/api/evidence` both enumerate. The `confirm()` its docstring cited
    lived in the apply UI, and that UI was removed."""

    def setUp(self):
        self.client = app.test_client()

    def _post(self, headers=None):
        with mock.patch.object(bugzilla_apply, "apply_recorded_actions",
                               return_value=[]) as ap:
            rv = self.client.post("/api/evidence/apply",
                                  json={"uuid": "u-1", "indices": [0]},
                                  headers=headers or {})
        return rv, ap

    def test_no_token_configured_refuses_rather_than_allows(self):
        # An unset secret must not mean "no authentication required" on the one route that
        # can write to a bug tracker.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_WRITE_TOKEN", None)
            rv, ap = self._post()
        self.assertEqual(rv.status_code, 503)
        ap.assert_not_called()

    def test_missing_header_forbidden(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}):
            rv, ap = self._post()
        self.assertEqual(rv.status_code, 403)
        ap.assert_not_called()

    def test_wrong_token_forbidden(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}):
            rv, ap = self._post({"X-Clouseau-Token": "guess"})
        self.assertEqual(rv.status_code, 403)
        ap.assert_not_called()

    def test_correct_token_allowed(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}):
            rv, ap = self._post({"X-Clouseau-Token": "s3cret"})
        self.assertEqual(rv.status_code, 200)
        ap.assert_called_once()

    def test_read_route_is_still_open(self):
        # Only the WRITE route is gated; the panel's read API must keep working.
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=None):
            rv = self.client.get("/api/evidence?uuid=u-1")
        self.assertEqual(rv.status_code, 200)


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
            "corroborations": {"candidate_in_pushlog_window": True},
            "skeptic": [{"status": "pass", "claim_ref": "line_match", "note": "exact."}],
            "verdict": {"confidence": "probable", "p_worth_investigating": 0.8,
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
            "Clouseau analysis (automated, 80% worth investigating",
            "null deref of mFoo",
            "Suspected regressor: [abc123](", "([gh](", ") (bug 7) by Dev.",
            "Code references:",
            "What the automated skeptic pass checked",
            ":dev, can you have a look please?",
            # Last, so the reader has read the analysis before being told to distrust it.
            "Filed automatically by [Clouseau]",
        ]
        at = [c.find(x) for x in order]
        self.assertNotIn(-1, at, "missing section in {!r}".format(c))
        self.assertEqual(at, sorted(at), "sections out of order")

    def test_code_reference_urls_have_no_spaces_in_the_revision(self):
        """Bug 2061961 shipped this to Bugzilla:

            [servo/components/style/data.rs](https://hg.mozilla.org/mozilla-central/
             file/tip (channel nightly)/servo/components/style/data.rs)

        The revision component was prose. The schema now reduces a citation's node to a
        real revision, so the end of that pipeline produces a link that resolves; asserted
        here rather than only at the schema because this is where the reader sees it."""
        from crashclouseau.agent.schema import RefCitation

        cite = RefCitation(
            kind="ref", node="tip (channel nightly)",
            filename="servo/components/style/data.rs",
        ).model_dump()
        refs = report_bug.build_code_references(
            {"mechanism": {"statement": "m", "citations": [cite]}}, "nightly")
        self.assertIn(
            "(https://hg.mozilla.org/mozilla-central/file/tip/"
            "servo/components/style/data.rs)", refs)
        url = refs[refs.index("](") + 2:refs.rindex(")")]
        self.assertNotIn(" ", url)

    def test_a_citation_whose_node_is_junk_still_links_by_permalink(self):
        # Emptying the node must not lose the reference: the permalink branch takes over.
        from crashclouseau.agent.schema import RefCitation

        cite = RefCitation(
            kind="ref", node="hg-changeset-metadata",
            filename="servo/components/style/data.rs",
            permalink="https://searchfox.org/firefox-main/source/servo/x.rs",
        ).model_dump()
        refs = report_bug.build_code_references(
            {"mechanism": {"statement": "m", "citations": [cite]}}, "nightly")
        self.assertIn("https://searchfox.org/firefox-main/source/servo/x.rs", refs)
        self.assertNotIn("hg-changeset-metadata", refs)

    def test_the_related_bugs_note_sits_before_the_needinfo_ask(self):
        # The filer skips past an open bug only when that bug predates the suspected
        # regressor (bug 1798397, open since 2022, versus a changeset that landed 1375 days
        # later). The reason belongs in the bug, ahead of the ask, so the triager weighing a
        # duplicate reads it first.
        info = {"uuid": "u-1", "channel": "nightly", "buildid": "20260808093004"}
        c = report_bug.build_bug_comment(
            info, self._stack3(), {"candidate": {"node": "n"}, "verdict": {}},
            needinfo=":dev, can you have a look please?", related_bugs=[1798397])
        self.assertLess(c.find("bug 1798397"), c.find(":dev, can you have a look"))
        self.assertIn("Please duplicate if that is wrong.", c)

    def test_the_related_bugs_note_is_absent_by_default(self):
        # Every hand-drafted bug and every page preview goes through here with no related
        # bugs; none of them may grow a dangling "Filed as a new bug rather than a comment on".
        for related in (None, [], [None]):
            with self.subTest(related=related):
                self.assertEqual(report_bug.build_related_bugs_note(related), "")

    def test_the_related_bugs_note_agrees_with_itself_in_the_plural(self):
        one = report_bug.build_related_bugs_note([1798397])
        two = report_bug.build_related_bugs_note([1798397, 1900000])
        self.assertIn("bug 1798397 — which is open", one)
        self.assertIn("it was filed before", one)
        self.assertIn("bug 1798397, bug 1900000 — which are open", two)
        self.assertIn("they were filed before", two)

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

    def test_resolve_pc_refuses_another_applications_component(self):
        # A mozilla-central changeset written FOR a Thunderbird bug is a normal thing — that is
        # most of what MailNews Core is — and inheriting its component drops a Firefox crash on
        # Thunderbird's triage queue. Unresolvable is the right answer: autofile_bug then
        # refuses to file at all, which this module prefers to filing into the wrong component.
        with mock.patch.object(report_bug, "_bugs_product_component",
                               return_value={123: ("MailNews Core", "Networking: Exchange")}), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author", return_value=[]):
            pc = report_bug.resolve_product_component(
                {"bug": 123, "node": "n"}, "nightly", "Firefox")
        self.assertEqual(pc, (None, None))

    def test_resolve_pc_drops_foreign_pairs_from_the_author_fallback(self):
        # The fallback tallies the author's recent bugs, and a comm-central-adjacent author's
        # most FREQUENT component is exactly the one that must not win.
        def fake_pc(bugids):
            bugids = list(bugids)
            if bugids == [123]:
                return {}                                  # regressor unreadable
            return {1: ("MailNews Core", "Backend"), 2: ("MailNews Core", "Backend"),
                    3: ("Core", "DOM: Navigation")}
        with mock.patch.object(report_bug, "_bugs_product_component", side_effect=fake_pc), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author",
                           return_value=[1, 2, 3]):
            pc = report_bug.resolve_product_component(
                {"bug": 123, "node": "n", "author": "Dev <dev@x.com>"}, "nightly", "Firefox")
        self.assertEqual(pc, ("Core", "DOM: Navigation"))

    def test_resolve_pc_with_no_crash_product_exempts_nobody(self):
        # The gate must not be switchable off by an absent product (page previews and old
        # dossiers both reach here without one).
        with mock.patch.object(report_bug, "_bugs_product_component",
                               return_value={123: ("Thunderbird", "General")}), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author", return_value=[]):
            self.assertEqual(
                report_bug.resolve_product_component({"bug": 123, "node": "n"}, "nightly"),
                (None, None))

    def test_the_other_app_note_cross_references_the_bug_it_filed_past(self):
        note = report_bug.build_other_app_bugs_note(
            [{"id": 2057980, "product": "MailNews Core"}])
        self.assertIn("bug 2057980 references this signature too", note)
        self.assertIn("it is filed in MailNews Core", note)
        self.assertIn("please duplicate if it is the same defect", note)

    def test_the_other_app_note_is_absent_by_default(self):
        # Every page preview and every hand-drafted bug comes through here with none.
        for other in (None, [], [None], [{}]):
            with self.subTest(other=other):
                self.assertEqual(report_bug.build_other_app_bugs_note(other), "")

    def test_the_other_app_note_agrees_with_itself_in_the_plural(self):
        two = report_bug.build_other_app_bugs_note(
            [{"id": 1, "product": "MailNews Core"}, {"id": 2, "product": "SeaMonkey"}])
        self.assertIn("bug 1, bug 2 reference this signature too", two)
        self.assertIn("they are filed in MailNews Core, SeaMonkey", two)
        self.assertIn("other applications", two)

    def test_the_draft_url_keeps_socorros_product_over_a_foreign_one(self):
        # The legacy hand-draft path: Socorro pre-fills the product from the CRASH, and the
        # regressor bug may only refine it, never move it to another application.
        socorro = {"product": ["Firefox"], "component": ["General"], "keywords": ["crash"]}
        foreign = {"bugs": [{"product": "MailNews Core", "component": "Networking: Exchange",
                             "assigned_to": "dev@x.com"}]}
        query = dict(socorro)
        self.assertEqual(report_bug.improve(query, foreign, 123, product="Firefox"), "dev@x.com")
        self.assertEqual((query["product"], query["component"]), (["Firefox"], ["General"]))
        self.assertEqual(query["blocked"], "clouseau,123")      # the rest still happens
        ours = {"bugs": [{"product": "Core", "component": "DOM: Navigation",
                          "assigned_to": "dev@x.com"}]}
        query = dict(socorro)
        report_bug.improve(query, ours, 123, product="Firefox")
        self.assertEqual((query["product"], query["component"]), ("Core", "DOM: Navigation"))

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
            "corroborations": {"candidate_in_pushlog_window": True},
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
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value={"exists": True, "nick": "bznick"}) as bz:
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
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value={"exists": True, "nick": ""}):
            prev = report_bug.build_bug_preview(ui, self._stack3(), dossier)
        self.assertEqual(prev["blocked"], ["clouseau"])

    def test_needinfo_person_uses_the_bugzilla_account(self):
        # email/name from the hgauthor record; the ACCOUNT and its nick from Bugzilla. The
        # hg address here IS a login, so rung 1 answers and no bug is read.
        with mock.patch("crashclouseau.models.Node.authors_for",
                        return_value={"n": {"nick": "hgnick", "real": "Dev", "email": "d@x"}}), \
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value={"exists": True, "nick": "stransky"}) as bz:
            p = report_bug._needinfo_person({"node": "n", "author": "Ignored <i@x>"}, "nightly")
        self.assertEqual(p["nick"], "stransky")   # bugzilla nick, NOT the hg "hgnick"
        self.assertEqual(p["account"], "d@x")     # the login the flag will name
        bz.assert_called_once_with("d@x")
        # no DB record, and the address is nobody on BMO with no bug to fall back to ->
        # name/email still describe the human for the PROSE, but there is no account, so
        # nothing goes in the flag.
        with mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value={"exists": False, "nick": ""}):
            p = report_bug._needinfo_person(
                {"author": "Real Name <real@x.com>"}, "nightly")
        self.assertEqual((p["nick"], p["name"], p["email"]), ("", "Real Name", "real@x.com"))
        self.assertEqual(p["account"], "")

    def test_bugzilla_user_lookup(self):
        """``exists`` is the point: a real account with no nick is NOT the same as no
        account, and only the second may not be put in a needinfo flag."""
        report_bug._USER_CACHE.clear()
        captured = {"constructions": 0}

        # Faithful to the real libmozdata API: BugzillaUser has NO get_data(); the query is
        # fired in the constructor and the handlers run when wait() drains it. Passing a
        # fault_user_handler is what makes libmozdata send `permissive`, so an unknown name
        # arrives as a FAULT instead of an exception.
        class FakeBZUser:
            reply = {"user": {"name": "stransky@x.com", "nick": "stransky"}}

            def __init__(self, user_names=None, include_fields=None, user_handler=None,
                         fault_user_handler=None, user_data=None, **kw):
                captured["names"] = user_names
                captured["fault_handler"] = fault_user_handler is not None
                captured["constructions"] += 1
                self._h, self._f, self._data = user_handler, fault_user_handler, user_data

            def wait(self):
                if "user" in self.reply:
                    self._h(self.reply["user"], self._data)
                else:
                    self._f(self.reply["fault"], self._data)
                return self

        with mock.patch.object(report_bug, "BugzillaUser", FakeBZUser):
            u = report_bug._bugzilla_user("stransky@x.com")
            u2 = report_bug._bugzilla_user("stransky@x.com")      # served from cache
        self.assertEqual(u, {"exists": True, "nick": "stransky"})
        self.assertEqual(u2, u)
        self.assertEqual(captured["names"], ["stransky@x.com"])
        self.assertTrue(captured["fault_handler"])                # => permissive
        self.assertEqual(captured["constructions"], 1)            # cached: one lookup

        # A name BMO does not know comes back as a fault: exists False, and the caller must
        # never put it in a flag.
        FakeBZUser.reply = {"fault": {"name": "farre@mozilla.com", "faultString": "nope"}}
        with mock.patch.object(report_bug, "BugzillaUser", FakeBZUser):
            self.assertEqual(report_bug._bugzilla_user("farre@mozilla.com"),
                             {"exists": False, "nick": ""})

        # A real account with NO nick still exists -- the old lookup could not tell these
        # apart, and conflating them is what would drop a usable needinfo.
        FakeBZUser.reply = {"user": {"name": "nonick@x.com", "nick": ""}}
        with mock.patch.object(report_bug, "BugzillaUser", FakeBZUser):
            self.assertEqual(report_bug._bugzilla_user("nonick@x.com"),
                             {"exists": True, "nick": ""})

        self.assertEqual(report_bug._bugzilla_user(""), {"exists": False, "nick": ""})
        report_bug._USER_CACHE.clear()

    def test_bugzilla_user_lookup_failure_is_not_a_missing_user(self):
        """A transport failure must not be read as "no such account" -- that would silently
        drop every needinfo whenever BMO hiccups. Unverified addresses stay usable; the
        create's own fallback carries the risk."""
        report_bug._USER_CACHE.clear()

        class Boom:
            def __init__(self, **kw):
                raise RuntimeError("connection reset")

        with mock.patch.object(report_bug, "BugzillaUser", Boom):
            u = report_bug._bugzilla_user("who@x.com")
        self.assertTrue(u["exists"])
        self.assertTrue(u["unverified"])
        self.assertNotIn("who@x.com", report_bug._USER_CACHE)   # not cached: retry later
        report_bug._USER_CACHE.clear()

    def test_needinfo_line_prefers_nick_then_name(self):
        self.assertEqual(report_bug._needinfo_line({"nick": "foo"}),
                         ":foo, can you have a look please?")
        self.assertEqual(report_bug._needinfo_line({"nick": "", "name": "Foo Bar"}),
                         "Foo Bar, can you have a look please?")
        self.assertIsNone(report_bug._needinfo_line({}))
        self.assertIsNone(report_bug._needinfo_line({"nick": "", "name": "", "email": ""}))

    def test_explanation_comment_regressor_only(self):
        # No mechanism -> still names the changeset; nothing at all -> None.
        exp = report_bug._explanation_comment(
            {}, {"node": "abc", "bug": 7},
            corroborations={"candidate_in_pushlog_window": True})
        self.assertIn("Suspected regressor: abc (bug 7)", exp)
        self.assertIsNone(report_bug._explanation_comment({}, {}))

    def test_a_candidate_from_outside_the_window_is_not_called_a_regressor(self):
        # Bug 2062119 named a changeset from 2022-12-13 as the "Suspected regressor" of an
        # August 2026 nightly crash, with the `regression` keyword and a blocks-link, while the
        # run's own skeptic pass was recording "a pre-existing latent race, not a new
        # regression". The reviewer's first reply was "I do not think bug 1768581 is the
        # regressor" -- and then he found the real one and wrote the patches. Ask for that.
        exp = report_bug._explanation_comment(
            {}, {"node": "abc", "bug": 7},
            corroborations={"candidate_in_pushlog_window": False})
        self.assertNotIn("Suspected regressor", exp)
        self.assertIn("Starting point — NOT a suspected cause: abc (bug 7)", exp)
        self.assertIn("did not land in this build's pushlog window", exp)
        self.assertIn("most useful thing you could leave on this bug", exp)

    def test_an_unrecorded_window_never_licenses_the_regression_claim(self):
        # Old dossiers and offline runs carry no flag. An unproven regression claim is the thing
        # being fixed, so silence must read as "no", not as "sure, go ahead".
        for corrob in (None, {}, {"something_else": True}):
            with self.subTest(corrob=corrob):
                exp = report_bug._explanation_comment({}, {"node": "abc"},
                                                      corroborations=corrob)
                self.assertNotIn("Suspected regressor", exp)
                self.assertFalse(report_bug.is_suspected_regression(corrob))

    def test_the_calibrated_number_replaces_the_rung_name(self):
        # "confidence high" reads as "I am sure this is the cause". The number the pipeline
        # actually calibrated is p_worth_investigating, fit at PERSON level.
        exp = report_bug._explanation_comment(
            {"confidence": "high", "p_worth_investigating": 0.9714,
             "mechanism": {"statement": "null deref"}}, {})
        self.assertIn("97% worth investigating", exp)
        self.assertIn("not that the changeset below caused it", exp)
        self.assertNotIn("confidence high", exp)

    def test_no_calibrated_number_claims_nothing(self):
        # Deliberately no fallback to the rung name: with no calibrated figure the honest thing
        # is to say nothing, not to reach for the word that caused the problem.
        exp = report_bug._explanation_comment(
            {"confidence": "high", "mechanism": {"statement": "null deref"}}, {})
        self.assertIn("Clouseau analysis (automated)", exp)
        self.assertNotIn("worth investigating", exp)
        self.assertNotIn("high", exp)

    def test_the_skeptic_review_travels_with_the_bug(self):
        # It already existed, was already on crashstack.html, and was already dropped on the way
        # to Bugzilla -- including the one finding that mattered.
        block = report_bug.build_skeptic_block({"skeptic": [
            {"status": "pass", "claim_ref": "field_offset_match",
             "note": "mMimeService at offset 40 (0x28)."},
            {"status": "unverifiable", "claim_ref": "restyle_invalidation_gap",
             "note": "Could not find or rule out an nsChangeHint."},
            {"status": "pass", "claim_ref": "no_recent_regressor",
             "note": "a pre-existing latent race, not a new regression."},
        ]})
        self.assertIn("no_recent_regressor", block)
        self.assertIn("not a new regression", block)
        # Open questions first: they are what a reader can most usefully close, and the cap
        # must never drop one in favour of a confirmation.
        self.assertLess(block.find("restyle_invalidation_gap"), block.find("field_offset_match"))
        self.assertIn("a `pass` means the check succeeded", block)

    def test_the_skeptic_block_is_capped_without_losing_open_questions(self):
        items = [{"status": "pass", "claim_ref": "p{}".format(i)} for i in range(20)]
        items.append({"status": "unverifiable", "claim_ref": "the_open_one"})
        block = report_bug.build_skeptic_block({"skeptic": items})
        self.assertIn("the_open_one", block)
        self.assertEqual(block.count("\n- "), report_bug._MAX_SKEPTIC_ITEMS)

    def test_no_skeptic_findings_means_no_section(self):
        for d in (None, {}, {"skeptic": []}, {"skeptic": ["junk"]}):
            with self.subTest(d=d):
                self.assertEqual(report_bug.build_skeptic_block(d), "")


class TestNeedinfoAccount(unittest.TestCase):
    """Resolving a BUGZILLA login for the regressor's author.

    An hg commit address is not a Bugzilla account. When it is not one, BMO rejects the
    whole `create_bug` (code 51) and the crash gets no bug at all -- which is what happened
    to f6fe186b, whose hg author is `farre@mozilla.com` while the account is
    `afarre@mozilla.com`. The ladder here is the fix: ask the BUGS who the person is.
    """

    def setUp(self):
        report_bug._USER_CACHE.clear()
        report_bug._BUG_CACHE.clear()
        self.addCleanup(report_bug._USER_CACHE.clear)
        self.addCleanup(report_bug._BUG_CACHE.clear)

    @staticmethod
    def _person(email, real, nick=""):
        return {"email": email, "real": real, "nick": nick}

    def _resolve(self, candidate, name, user=None, people=None, others=None):
        user = user if user is not None else {"exists": False, "nick": ""}
        with mock.patch.object(report_bug, "_bugzilla_user", return_value=user), \
                mock.patch.object(report_bug, "_bug_people",
                                  side_effect=lambda ids: {b: (people or {}).get(b, [])
                                                           for b in ids}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author",
                           return_value=list(others or [])):
            return report_bug._needinfo_account(
                candidate, "nightly", candidate.get("_email", ""), name)

    def test_the_hg_address_is_used_when_it_is_an_account(self):
        # The common case: one user lookup, no bug read at all.
        with mock.patch.object(report_bug, "_bugzilla_user",
                               return_value={"exists": True, "nick": "jdm"}), \
                mock.patch.object(report_bug, "_bug_people") as bp:
            got = report_bug._needinfo_account(
                {"bug": 1, "node": "n"}, "nightly", "jdemooij@mozilla.com", "Jan de Mooij")
        self.assertEqual(got, {"email": "jdemooij@mozilla.com", "nick": "jdm"})
        bp.assert_not_called()

    def test_the_regressor_bugs_assignee_answers_when_hg_does_not(self):
        # The f6fe186b case, exactly: hg says farre@, BMO says nobody, bug 2042379 says
        # "Andreas Farre [:farre] <afarre@mozilla.com>".
        got = self._resolve(
            {"bug": 2042379, "node": "n", "_email": "farre@mozilla.com"},
            "Andreas Farre",
            people={2042379: [self._person("afarre@mozilla.com",
                                           "Andreas Farre [:farre]", "farre")]})
        self.assertEqual(got, {"email": "afarre@mozilla.com", "nick": "farre"})

    def test_a_private_regressor_bug_falls_back_to_another_one(self):
        # A restricted bug is simply ABSENT from a batched read -- here bug 7 returns no
        # people at all -- so we look at the author's other landings, newest first.
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "dev@x.com"}, "Some Dev",
            people={7: [], 9: [self._person("acct@x.com", "Some Dev [:sd]", "sd")]},
            others=[7, 9])
        self.assertEqual(got, {"email": "acct@x.com", "nick": "sd"})

    def test_the_newest_matching_bug_wins(self):
        # The stub deliberately answers in the WRONG order (8 before 9), the way a warm
        # `_BUG_CACHE` would: cache hits come out first. Iterating the response instead of
        # `others` would then pick the older bug, and nothing else in the suite would notice.
        with mock.patch.object(report_bug, "_bugzilla_user",
                               return_value={"exists": False, "nick": ""}), \
                mock.patch.object(report_bug, "_bug_people", return_value={
                    8: [self._person("old@x.com", "Some Dev")],
                    9: [self._person("new@x.com", "Some Dev")]}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author",
                           return_value=[9, 8]):        # newest-first
            got = report_bug._needinfo_account(
                {"bug": 7, "node": "n"}, "nightly", "dev@x.com", "Some Dev")
        self.assertEqual(got["email"], "new@x.com")

    def test_a_different_person_on_the_bug_is_never_used(self):
        # The bug is readable and has a perfectly good assignee -- who is someone else.
        # Needinfo-ing the wrong human is worse than needinfo-ing nobody.
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "dev@x.com"}, "Some Dev",
            people={7: [self._person("triager@x.com", "A Triager")]})
        self.assertEqual(got, {})

    def test_two_people_who_share_a_surname_are_not_confused(self):
        # prod hgauthors really does hold farre@mozilla.com "Andreas Farre" AND
        # sfarre@mozilla.com "Simon Farre".
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "sfarre@mozilla.com"}, "Simon Farre",
            people={7: [self._person("afarre@mozilla.com", "Andreas Farre [:farre]")]})
        self.assertEqual(got, {})

    def test_no_author_name_means_no_guessing(self):
        # Without a name there is nothing to match on, and an unverified assignee is a
        # coin flip. 3 of 975 prod hgauthors have no real name.
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "dev@x.com"}, "",
            people={7: [self._person("someone@x.com", "Someone Else")]})
        self.assertEqual(got, {})

    def test_an_unverifiable_address_yields_to_a_name_verified_account(self):
        """When BMO will not answer the user lookup we do not know whether the hg address is
        a login. A bug-verified account is better evidence than that guess, so the unverified
        address must not short-circuit rungs 2 and 3."""
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "farre@mozilla.com"}, "Andreas Farre",
            user={"exists": True, "nick": "", "unverified": True},
            people={7: [self._person("afarre@mozilla.com", "Andreas Farre [:farre]",
                                     "farre")]})
        self.assertEqual(got["email"], "afarre@mozilla.com")

    def test_an_unverifiable_address_is_still_better_than_nobody(self):
        # ...but if the bugs identify no one, try it anyway: the create drops the flag and
        # keeps the bug if BMO refuses it.
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "maybe@x.com"}, "Some Dev",
            user={"exists": True, "nick": "", "unverified": True})
        self.assertEqual(got, {"email": "maybe@x.com", "nick": ""})

    def test_a_known_non_account_is_never_used_as_a_last_resort(self):
        # The difference that matters: BMO SAID this is nobody, so using it would cost the
        # whole bug. Only an unverifiable address gets the benefit of the doubt.
        got = self._resolve({"bug": 7, "node": "n", "_email": "farre@mozilla.com"},
                            "Andreas Farre", user={"exists": False, "nick": ""})
        self.assertEqual(got, {})

    def test_the_nick_matches_an_hg_name_that_is_a_login(self):
        """Measured: hg records some authors' "real name" as their login — `longsonr`,
        whose account is "Robert Longson [:longsonr]". The name key cannot see them; the
        nick key can, and it added 6 points (59% -> 65%) over 189 prod pairs with zero
        cases of it disagreeing with the name key."""
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "longsonr@gmail.com"}, "longsonr",
            people={7: [self._person("longsonr@gmail.com",
                                     "Robert Longson [:longsonr]", "longsonr")]})
        self.assertEqual(got["email"], "longsonr@gmail.com")

    def test_the_same_address_is_conclusive_even_with_a_different_name(self):
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "evilpies@gmail.com"}, "Tom Schuster",
            people={7: [self._person("evilpies@gmail.com",
                                     "Tom S. (please needinfo tschuster)", "evilpies")]})
        self.assertEqual(got["email"], "evilpies@gmail.com")

    def test_a_bare_local_part_across_domains_is_NOT_a_match(self):
        """Deliberately excluded. It would have taken the rate to 74%, but 10 of the 17 it
        adds are `moz-wptsync-bot` (`wptsync@mozilla.com` vs `wptsync@mozilla.bugs`), and we
        must never ask a bot to investigate a crash. Across domains a bare local part is
        also weak evidence that two addresses are one human."""
        got = self._resolve(
            {"bug": 7, "node": "n", "_email": "wptsync@mozilla.com"}, "moz-wptsync-bot",
            people={7: [self._person("wptsync@mozilla.bugs",
                                     "Web Platform Test Sync Bot [:wpt-sync]", "wpt-sync")]})
        self.assertEqual(got, {})

    def test_nothing_resolves_to_no_account(self):
        self.assertEqual(self._resolve({"bug": 7, "node": "n"}, "Nobody Known"), {})
        self.assertEqual(report_bug._needinfo_account({}, "nightly", "", ""), {})

    def test_name_normalisation(self):
        # BMO decorates real_name with the nick; hg does not.
        self.assertEqual(report_bug._norm_name("Andreas Farre [:farre]"), "andreas farre")
        self.assertEqual(report_bug._norm_name("Andreas  Farre"), "andreas farre")
        self.assertEqual(report_bug._norm_name("  Jan de Mooij (:jandem)  "), "jan de mooij")
        self.assertEqual(report_bug._norm_name(None), "")
        # Annotations are not always LAST, and there is often more than one. An end-anchored
        # strip would leave "andreas farre [:farre] (pto until monday)" and match nobody.
        self.assertEqual(report_bug._norm_name("Andreas Farre [:farre] (PTO until Monday)"),
                         "andreas farre")
        self.assertEqual(report_bug._norm_name("[:jandem] Jan de Mooij"), "jan de mooij")
        self.assertEqual(report_bug._norm_name("Foo Bar [:foo][:bar]"), "foo bar")
        # ...but stripping must not merge two people: an exact full-name match still stands
        # between Andreas and Simon.
        self.assertNotEqual(report_bug._norm_name("Andreas Farre [:farre]"),
                            report_bug._norm_name("Simon Farre [:sfarre]"))
        # An empty name must never match another empty name.
        self.assertIsNone(report_bug._match_author([{"real": ""}], ""))
        self.assertIsNone(report_bug._match_author([{"real": ""}], "Dev"))

    def test_bug_people_skips_the_unassigned_placeholder(self):
        got = {}

        class FakeBZ:
            def __init__(self, bugids=None, include_fields=None, bughandler=None,
                         bugdata=None, **kw):
                for b in bugids:
                    bughandler({
                        "id": int(b),
                        "assigned_to_detail": {"email": "nobody@mozilla.org",
                                               "real_name": "Nobody; OK to take it"},
                        "creator_detail": {"email": "filer@x.com", "real_name": "The Filer",
                                           "nick": "filer"},
                    }, bugdata)

            def get_data(self):
                return self

            def wait(self):
                return self

        with mock.patch.object(report_bug, "Bugzilla", FakeBZ):
            got = report_bug._bug_people([11])
        self.assertEqual(got[11], [{"email": "filer@x.com", "real": "The Filer",
                                    "nick": "filer"}])

    def test_bug_people_caches_the_unreadable_answer_too(self):
        # A security bug never becomes readable; re-asking every preview buys nothing.
        calls = []

        class FakeBZ:
            def __init__(self, bugids=None, **kw):
                calls.append(list(bugids))

            def get_data(self):
                return self

            def wait(self):
                return self

        with mock.patch.object(report_bug, "Bugzilla", FakeBZ):
            self.assertEqual(report_bug._bug_people([2043188]), {2043188: []})
            self.assertEqual(report_bug._bug_people([2043188]), {2043188: []})
        self.assertEqual(len(calls), 1)

    def test_the_request_asks_for_the_base_fields_not_the_detail_companions(self):
        """The one thing a response fake cannot catch, and it was a real bug: asking for
        `assigned_to_detail` returns NOTHING. Bugzilla emits the detail hash as a companion
        of `assigned_to`, and `filter_wants` does not recognise `assigned_to_detail` as a
        token (its prefix branch wants `assigned_to.`, with a dot). Every people-shaped fake
        in this file fabricates the detail fields regardless of what was requested, so only
        an assertion on the REQUEST can pledge this."""
        seen = {}

        class FakeBZ:
            def __init__(self, bugids=None, include_fields=None, bughandler=None,
                         bugdata=None, **kw):
                seen["fields"] = list(include_fields or [])

            def get_data(self):
                return self

            def wait(self):
                return self

        with mock.patch.object(report_bug, "Bugzilla", FakeBZ):
            report_bug._bug_meta([1])
        self.assertIn("assigned_to", seen["fields"])
        self.assertIn("creator", seen["fields"])
        self.assertNotIn("assigned_to_detail", seen["fields"])
        self.assertNotIn("creator_detail", seen["fields"])
        # product/component still come from the same one fetch
        self.assertIn("product", seen["fields"])
        self.assertIn("component", seen["fields"])

    def test_a_lookup_failure_never_raises_and_is_never_cached(self):
        """"Could not ASK" is not "unreadable". Caching a transport failure as an empty
        answer would make one BMO blip permanently blind this process to that bug."""
        calls = []

        class Boom:
            def __init__(self, bugids=None, **kw):
                calls.append(list(bugids))
                raise RuntimeError("bugzilla down")

        with mock.patch.object(report_bug, "Bugzilla", Boom):
            self.assertEqual(report_bug._bug_people([5]), {})
            self.assertEqual(report_bug._BUG_CACHE, {})       # nothing remembered
            self.assertEqual(report_bug._bug_people([5]), {})
        self.assertEqual(len(calls), 2)                        # so it is asked again
