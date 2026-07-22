# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# P1 off-stack seeding + pinned mode + precision gates.
# DATABASE_URL=sqlite:// python -m unittest tests.test_offstack
import asyncio
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    CallEdge,
    CallPath,
    Candidate,
    Claim,
    Confidence,
    CrashBrief,
    Decision,
    DiffLineCitation,
    Dossier,
    FailureClass,
    SearchfoxCitation,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="A::b", repo="mozilla-central"
)


def _run(coro):
    return asyncio.run(coro)


def _offstack_cfg(**over):
    base = {
        "enabled": True,
        "max_candidates": 150,
        "pinned": True,
        "require_callpath_for_strong": True,
        "exposer_classifier": True,
        "observe_only": True,
    }
    base.update(over)
    return base


def _dt(day):
    return datetime(2026, 7, day, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class TestConfigOffstack(unittest.TestCase):
    def test_defaults_off_but_guards_on(self):
        cfg = config.get_agent_offstack()
        self.assertFalse(cfg["enabled"])            # feature OFF by default
        self.assertTrue(cfg["pinned"])
        self.assertTrue(cfg["require_callpath_for_strong"])
        self.assertTrue(cfg["exposer_classifier"])
        self.assertTrue(cfg["observe_only"])
        self.assertEqual(cfg["max_candidates"], 150)

    def test_env_lever_enables(self):
        with mock.patch.dict(os.environ, {"OFFSTACK_ENABLED": "1"}):
            self.assertTrue(config.get_agent_offstack()["enabled"])
        with mock.patch.dict(os.environ, {"OFFSTACK_OBSERVE_ONLY": "no"}):
            self.assertFalse(config.get_agent_offstack()["observe_only"])

    def test_offstack_cost_cap_higher_than_default(self):
        self.assertGreaterEqual(config.get_agent_offstack_cost_cap(), 4.0)


# --------------------------------------------------------------------------- #
# Tokenizer + poison detector
# --------------------------------------------------------------------------- #
class TestTokens(unittest.TestCase):
    def test_camelcase_and_prose_share_vocab(self):
        sig = orch._tokens("mozilla::detail::nsTStringRepr::Length")
        desc = orch._tokens("Bug 1 - Fix nsTStringRepr length handling")
        self.assertTrue(sig & desc)                 # non-empty overlap
        self.assertIn("length", sig)
        self.assertIn("string", sig)
        self.assertNotIn("bug", desc)               # stoplisted


class TestLooksPoison(unittest.TestCase):
    def test_poison_patterns(self):
        self.assertTrue(orch._looks_poison(0xE5E5E5E5))
        self.assertTrue(orch._looks_poison(0xE5E5E5ED))   # +offset off-byte allowed
        self.assertTrue(orch._looks_poison(0xDDDDDDDD))
        self.assertTrue(orch._looks_poison(0x5A5A5A5A5A5A5A5A))

    def test_non_poison(self):
        self.assertFalse(orch._looks_poison(0x8))         # small -> field-offset domain
        self.assertFalse(orch._looks_poison(0x0))
        self.assertFalse(orch._looks_poison(0xDEADBEEF))  # varied bytes
        self.assertFalse(orch._looks_poison(None))

    def test_two_byte_needs_both_poison(self):
        # A 2-byte address must not qualify on a SINGLE poison byte (would spuriously
        # demote a genuine culprit whose fault is e.g. 0xE512).
        self.assertFalse(orch._looks_poison(0xE512))
        self.assertFalse(orch._looks_poison(0x2BFF))
        self.assertTrue(orch._looks_poison(0xE5E5))       # both bytes poison -> real


# --------------------------------------------------------------------------- #
# Off-stack candidate enumeration
# --------------------------------------------------------------------------- #
class TestOffstackCandidates(unittest.TestCase):
    def _window(self):
        return [
            {"node": "match1", "date": _dt(1), "backedout": False, "merge": False,
             "bug": 111, "desc": "Bug 111 - Fix nsTStringRepr length off-by-one"},
            {"node": "recent", "date": _dt(10), "backedout": False, "merge": False,
             "bug": -1, "desc": "Bug 222 - unrelated telemetry probe"},
            {"node": "backout", "date": _dt(11), "backedout": True, "merge": False,
             "bug": 333, "desc": "Backed out changeset for nsTStringRepr regression"},
            {"node": "merged", "date": _dt(11), "backedout": False, "merge": True,
             "bug": -1, "desc": "Merge autoland to central"},
        ]

    def _ui(self):
        return {"uuid": "u-1", "channel": "nightly", "product": "Firefox",
                "buildid": _dt(12), "node": "buildnode", "signature": "nsTStringRepr::Length"}

    def test_ranked_deduped_shaped(self):
        with mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=self._window()) as pl:
            out = orch._offstack_candidates(self._ui(), _offstack_cfg())
        # merges dropped; sig-overlap first, then recency, backouts last.
        self.assertEqual([c["node"] for c in out], ["match1", "recent", "backout"])
        first = out[0]
        self.assertIsNone(first["score"])            # no proximity score off-stack
        self.assertFalse(first["noise"])
        self.assertEqual(first["bug"], 111)
        self.assertIn("nsTStringRepr", first["desc"])
        # bug -1 maps to None
        self.assertIsNone(out[1]["bug"])
        # the window query dropped the extensions filter (passthrough file_filter)
        self.assertTrue(pl.called)
        _, kw = pl.call_args
        self.assertTrue(callable(kw["file_filter"]))
        self.assertTrue(kw["file_filter"]("anything.random"))   # passthrough = keep all

    def test_prefers_build_node_as_tochange(self):
        with mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=[]) as pl:
            orch._offstack_candidates(self._ui(), _offstack_cfg())
        self.assertEqual(pl.call_args.args[0], "r0")        # startrev = last-good
        self.assertEqual(pl.call_args.args[1], "buildnode")  # tochange = crash build node

    def test_cap_applied(self):
        big = [{"node": f"n{i}", "date": _dt(1), "backedout": False, "merge": False,
                "bug": -1, "desc": f"c{i}"} for i in range(50)]
        with mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=big):
            out = orch._offstack_candidates(self._ui(), _offstack_cfg(max_candidates=5))
        self.assertEqual(len(out), 5)

    def test_date_window_fallback_when_predecessor_missing(self):
        # Only one build known -> fall back to the date-window pushlog.
        with mock.patch.object(orch.models.Build, "get_two_last", return_value=[{"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs") as pfr, \
             mock.patch("crashclouseau.pushlog.pushlog", return_value=[]) as pdate:
            orch._offstack_candidates(self._ui(), _offstack_cfg())
        pfr.assert_not_called()
        pdate.assert_called_once()

    def test_hg_failure_returns_empty(self):
        with mock.patch.object(orch.models.Build, "get_two_last",
                               side_effect=RuntimeError("hg down")):
            self.assertEqual(orch._offstack_candidates(self._ui(), _offstack_cfg()), [])


# --------------------------------------------------------------------------- #
# build_seed off-stack branch
# --------------------------------------------------------------------------- #
class TestBuildSeedOffstack(unittest.TestCase):
    _RES = {"frames": [{"stackpos": 0, "function": "F", "filename": "a.cpp",
                        "line": 1, "changesets": {}}]}       # no scored changesets
    _UI = {"uuid": "u-1", "id": 1, "signature": "nsTStringRepr::Length",
           "buildid": _dt(12), "channel": "nightly", "product": "Firefox",
           "java": False, "node": "buildnode123"}

    def test_disabled_returns_none(self):
        # default config -> off-stack disabled -> preserve today's skip behavior
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                               return_value=(self._RES, self._UI)):
            self.assertIsNone(orch.build_seed("u-1"))

    def test_enabled_seeds_window(self):
        window = [{"node": "w1", "date": _dt(3), "backedout": False, "merge": False,
                   "bug": 555, "desc": "Bug 555 - touch nsTStringRepr"}]
        with mock.patch.object(orch.config, "get_agent_offstack", return_value=_offstack_cfg()), \
             mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                               return_value=(self._RES, self._UI)), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "nsTStringRepr::Length",
                                             "channel": "nightly", "product": "Firefox",
                                             "buildid": "x", "version": "1"}), \
             mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=window), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}), \
             mock.patch("crashclouseau.inspector.git2hg", return_value="hgbuildnode"):
            seed = orch.build_seed("u-1")
        self.assertTrue(seed["is_offstack"])
        self.assertEqual(seed["build_node"], "buildnode123")
        self.assertEqual(seed["pin_rev"], "buildnode123")     # pinned (config pinned=True)
        self.assertEqual([c["node"] for c in seed["candidates"]], ["w1"])
        self.assertIsNone(seed["candidates"][0]["score"])
        self.assertIn("nsTStringRepr", seed["candidates"][0]["desc"])

    def test_pin_rev_empty_when_pinning_off(self):
        window = [{"node": "w1", "date": _dt(3), "backedout": False, "merge": False,
                   "bug": 1, "desc": "x"}]
        with mock.patch.object(orch.config, "get_agent_offstack",
                               return_value=_offstack_cfg(pinned=False)), \
             mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                               return_value=(self._RES, self._UI)), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=window), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertTrue(seed["is_offstack"])
        self.assertEqual(seed["pin_rev"], "")             # pinning disabled

    def test_empty_window_returns_none(self):
        with mock.patch.object(orch.config, "get_agent_offstack", return_value=_offstack_cfg()), \
             mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                               return_value=(self._RES, self._UI)), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Build, "get_two_last", return_value=[]), \
             mock.patch("crashclouseau.pushlog.pushlog", return_value=[]), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            self.assertIsNone(orch.build_seed("u-1"))

    def test_onstack_candidates_unaffected_but_now_pinned(self):
        # a crash WITH scored changesets never enters the off-stack candidate path, but its
        # blame/source reads are now PINNED to the build rev (on-stack precision improvement).
        res = {"frames": [{"stackpos": 0, "function": "F", "filename": "a.cpp",
                           "line": 1, "changesets": {"abc": {"score": 3}}}]}
        with mock.patch.object(orch.config, "get_agent_offstack", return_value=_offstack_cfg()), \
             mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, self._UI)), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "F", "channel": "nightly",
                                             "product": "Firefox", "buildid": "x", "version": "1"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}), \
             mock.patch("crashclouseau.inspector.git2hg", return_value="hgbuildnode"):
            seed = orch.build_seed("u-1")
        self.assertFalse(seed["is_offstack"])
        self.assertEqual(seed["pin_rev"], "buildnode123")   # on-stack now pins to the build
        self.assertEqual(seed["candidates"][0]["node"], "abc")


# --------------------------------------------------------------------------- #
# SF-3 call-path gate
# --------------------------------------------------------------------------- #
def _strong(callpath_edges=None, candidate=True):
    return Dossier(
        candidate=Candidate(node="abc123def456", bug=42) if candidate else None,
        call_path=CallPath(edges=callpath_edges) if callpath_edges is not None else None,
        verdict=Verdict(
            decision=Decision.strong_evidence, confidence=Confidence.high,
            mechanism=Claim(statement="m", citations=[_SF]),
            consistency=Claim(statement="c", citations=[_SF]),
        ),
    )


_SF_EDGE = CallEdge(caller_symbol="A::a", callee_symbol="A::b", via="calls-from", citations=[_SF])
_DIFF_EDGE = CallEdge(
    caller_symbol="A::a", callee_symbol="A::b", via="x",
    citations=[DiffLineCitation(node="n", filename="f", line=1, side="added", content="x")],
)


class TestCallpathGate(unittest.TestCase):
    def test_keeps_strong_with_verified_callpath(self):
        d = _strong(callpath_edges=[_SF_EDGE])
        orch._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertTrue(d.corroborations["call_path_verified"])

    def test_downgrades_to_lead_without_callpath(self):
        d = _strong(callpath_edges=[_DIFF_EDGE])   # edge but no searchfox citation
        orch._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_abstains_without_callpath_or_anchor(self):
        d = _strong(callpath_edges=None, candidate=False)  # no anchor at all
        orch._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_noop_on_non_strong(self):
        d = Dossier(candidate=Candidate(node="abc123def456"),
                    verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                    needinfo_draft="?"))
        orch._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.lead)


# --------------------------------------------------------------------------- #
# Exposer classifier
# --------------------------------------------------------------------------- #
class TestExposerClassifier(unittest.TestCase):
    def _seed(self, addr):
        return {"uuid": "u", "raw_crash": {"json_dump": {"crash_info": {"address": addr}}}}

    def test_poison_downgrades_strong_and_flags(self):
        d = _strong(callpath_edges=[_SF_EDGE])
        orch._classify_exposer(d, self._seed("0xe5e5e5e5"))
        self.assertEqual(d.verdict.decision, Decision.lead)      # strong signal -> downgrade
        self.assertTrue(d.corroborations["exposer_suspected"])
        self.assertTrue(d.corroborations["exposer_strong"])

    def test_weak_signal_flags_only_no_downgrade(self):
        # failure_class=uaf alone is a weak hint: annotate, but do NOT demote a culprit.
        d = _strong(callpath_edges=[_SF_EDGE])
        d.crash = CrashBrief(uuid="u", failure_class=FailureClass.uaf)
        orch._classify_exposer(d, self._seed("0x0"))            # not a poison addr
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertTrue(d.corroborations["exposer_suspected"])
        self.assertFalse(d.corroborations["exposer_strong"])

    def test_no_signals_no_flag(self):
        d = _strong(callpath_edges=[_SF_EDGE])
        orch._classify_exposer(d, self._seed("0x0"))
        self.assertNotIn("exposer_suspected", d.corroborations)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)


# --------------------------------------------------------------------------- #
# Observe-only
# --------------------------------------------------------------------------- #
class TestGateFlagsCoexist(unittest.TestCase):
    def test_corroboration_gate_preserves_earlier_flags(self):
        # SF-3 (and exposer) run BEFORE the corroboration gate; that gate must MERGE its
        # flags, not replace the dict, or call_path_verified / exposer_* get wiped from
        # the persisted corroborations.
        d = _strong(callpath_edges=[_SF_EDGE])
        orch._apply_callpath_gate(d, {"uuid": "u"})
        orch._classify_exposer(d, {"uuid": "u", "raw_crash":
                                   {"json_dump": {"crash_info": {"address": "0xe5e5e5e5"}}}})
        orch._apply_corroboration_gate(d, {"uuid": "u", "raw_crash": {}})
        self.assertTrue(d.corroborations["call_path_verified"])   # survived the merge
        self.assertTrue(d.corroborations["exposer_suspected"])    # survived the merge


class TestObserveOnly(unittest.TestCase):
    def test_clears_actions_and_flags(self):
        result = CrashTriageResult(
            num_turns=1, total_cost_usd=0.1, result="ok",
            dossier=_strong(callpath_edges=[_SF_EDGE]),
            actions=[{"type": "bugzilla.add_comment", "params": {"bug_id": 1, "text": "x"}}],
        )
        orch._apply_offstack_observe_only(result)
        self.assertEqual(result.actions, [])                    # nothing to apply
        self.assertTrue(result.dossier.corroborations["offstack_observe_only"])


# --------------------------------------------------------------------------- #
# History pinning
# --------------------------------------------------------------------------- #
class TestHistoryPin(unittest.TestCase):
    def test_pin_redirects_default_and_tip_to_build_rev(self):
        from crashclouseau.agent.tools import history as H
        ctx = H.HistoryCtx(channel="nightly", build_rev="buildrev")
        self.assertEqual(H._pin(ctx, ""), "buildrev")           # default -> build
        self.assertEqual(H._pin(ctx, "tip"), "buildrev")        # explicit tip -> build
        self.assertEqual(H._pin(ctx, "othernode"), "othernode")  # explicit node honored

    def test_pin_noop_without_build_rev(self):
        from crashclouseau.agent.tools import history as H
        ctx = H.HistoryCtx(channel="nightly")
        self.assertEqual(H._pin(ctx, ""), "tip")                # on-stack keeps tip
        self.assertEqual(H._pin(ctx, "n"), "n")

    def test_blame_reads_at_build_rev(self):
        from crashclouseau.agent.tools import history as H
        ctx = H.HistoryCtx(channel="nightly", build_rev="deadbeef")
        with mock.patch.object(H, "_resolve", side_effect=lambda n: n), \
             mock.patch.object(H.Annotate, "get", return_value={"f": {"annotate": []}}) as m:
            _run(H.blame(ctx, "f", 1))
        m.assert_called_once_with("f", "nightly", "deadbeef")   # pinned, not tip


# --------------------------------------------------------------------------- #
# Pinned source tool
# --------------------------------------------------------------------------- #
class TestSourceTool(unittest.TestCase):
    def test_raw_file_uses_pinned_rev_and_slices(self):
        from crashclouseau.agent.tools import source as S
        ctx = S.SourceCtx(channel="nightly", build_rev="buildrev")
        body = "\n".join(f"line{i}" for i in range(1, 21))
        with mock.patch.object(S, "_resolve", side_effect=lambda n: n), \
             mock.patch.object(S.hgedge, "raw_file", return_value=body) as m:
            out = _run(S.raw_file(ctx, "dom/base/x.cpp", line_start=3, line_end=5))
        m.assert_called_once_with("dom/base/x.cpp", "buildrev", "nightly")  # pinned
        self.assertIn("line3", out)
        self.assertIn("line5", out)
        self.assertNotIn("line6", out)

    def test_raw_file_missing_is_graceful(self):
        from crashclouseau.agent.tools import source as S
        ctx = S.SourceCtx(channel="nightly", build_rev="r")
        with mock.patch.object(S, "_resolve", side_effect=lambda n: n), \
             mock.patch.object(S.hgedge, "raw_file", return_value=None):
            out = _run(S.raw_file(ctx, "missing.cpp"))
        self.assertIn("not found", out)

    def test_explicit_tip_is_redirected_to_build_rev(self):
        # The whole point of the pinned tool: an explicit rev='tip' (or 'default') in a
        # pinned run must NOT leak tip — it is deterministically redirected to build_rev.
        from crashclouseau.agent.tools import source as S
        ctx = S.SourceCtx(channel="nightly", build_rev="buildrev")
        with mock.patch.object(S, "_resolve", side_effect=lambda n: n), \
             mock.patch.object(S.hgedge, "raw_file", return_value="x") as m:
            _run(S.raw_file(ctx, "f.cpp", rev="tip"))
            m.assert_called_once_with("f.cpp", "buildrev", "nightly")
            m.reset_mock()
            _run(S.raw_file(ctx, "f.cpp", rev="default"))
            m.assert_called_once_with("f.cpp", "buildrev", "nightly")
            m.reset_mock()
            _run(S.raw_file(ctx, "f.cpp", rev="explicitnode"))   # explicit non-tip honored
            m.assert_called_once_with("f.cpp", "explicitnode", "nightly")


class TestPinNode(unittest.TestCase):
    def test_shared_rule(self):
        from crashclouseau.agent.tools import pin_node
        self.assertEqual(pin_node("build", ""), "build")
        self.assertEqual(pin_node("build", "tip"), "build")
        self.assertEqual(pin_node("build", "default"), "build")
        self.assertEqual(pin_node("build", "other"), "other")
        self.assertEqual(pin_node("", ""), "tip")            # on-stack default
        self.assertEqual(pin_node("", "n"), "n")


class TestReconcileOffstackActions(unittest.TestCase):
    def _action(self, text, decision="strong-evidence"):
        return {
            "type": "bugzilla.add_comment",
            "params": {"bug_id": 42, "text": text, "is_private": True},
            "reasoning": "auto-drafted from the verdict's needinfo_draft ({}); "
                         "human-confirmed before apply".format(decision),
        }

    def test_downgraded_lead_gets_soft_draft_not_assertive(self):
        # SF-3 downgrades strong->lead; the persisted action must carry the SOFT lead draft,
        # not the original assertive strong-evidence text.
        d = _strong(callpath_edges=[_DIFF_EDGE])          # no searchfox edge -> SF-3 downgrades
        orch._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.lead)
        result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=d,
                                   actions=[self._action("ASSERTIVE: changeset X caused this")])
        orch._reconcile_bridged_action(result)
        self.assertEqual(len(result.actions), 1)
        text = result.actions[0]["params"]["text"]
        self.assertNotIn("ASSERTIVE", text)               # stale assertive draft dropped
        self.assertIn("not an accusation", text)          # soft lead draft substituted

    def test_downgraded_abstain_drops_action(self):
        d = _strong(callpath_edges=None, candidate=False)  # SF-3 -> abstain (no anchor)
        orch._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.abstain)
        result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=d,
                                   actions=[self._action("ASSERTIVE")])
        orch._reconcile_bridged_action(result)
        self.assertEqual(result.actions, [])              # abstain emits no action

    def test_preserves_genuine_recorder_action(self):
        d = _strong(callpath_edges=[_SF_EDGE])            # stays strong (verified call path)
        recorder_act = {"type": "bugzilla.update_bug", "params": {"bug_id": 9},
                        "reasoning": "agent recorded this directly"}
        result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=d,
                                   actions=[recorder_act, self._action("strong draft")])
        orch._reconcile_bridged_action(result)
        # recorder action kept; the auto-bridged one re-derived (strong verdict unchanged)
        self.assertIn(recorder_act, result.actions)


# --------------------------------------------------------------------------- #
# User prompt off-stack framing
# --------------------------------------------------------------------------- #
class TestUserPromptOffstack(unittest.TestCase):
    def test_offstack_framing_and_desc_and_no_cap(self):
        from crashclouseau.agent import triage
        cands = [{"node": f"n{i}", "score": None, "bug": None, "backedout": False,
                  "pushdate": None, "noise": False, "desc": f"desc number {i}"}
                 for i in range(30)]
        crash = {"uuid": "u", "signature": "S", "channel": "nightly", "stack": "#0 f a:1",
                 "is_offstack": True, "candidates": cands}
        out = triage._user_prompt(crash)
        self.assertIn("FULL pushlog window", out)
        self.assertIn("PINNED", out)
        self.assertIn("desc number 29", out)        # not truncated at 20 for off-stack
        self.assertIn("| desc number 0", out)       # desc rendered

    def test_onstack_keeps_cap_of_20(self):
        from crashclouseau.agent import triage
        cands = [{"node": f"n{i}", "score": i, "bug": None, "backedout": False,
                  "pushdate": None, "noise": False} for i in range(30)]
        crash = {"uuid": "u", "signature": "S", "channel": "nightly", "stack": "s",
                 "candidates": cands}
        out = triage._user_prompt(crash)
        self.assertIn("proximity", out)
        self.assertIn("n19", out)
        self.assertNotIn("n20", out)                # capped at 20 on-stack


class TestOffstackIngestion(unittest.TestCase):
    """The ingestion gate: an off-stack crash (frames present, NO scored changeset) is
    STORED + later enqueued only when OFFSTACK_ENABLED — otherwise it's dropped at ingestion
    (no crashstack, useless=True, never enqueued) and build_seed's off-stack branch is
    unreachable."""

    def _data(self):
        # a stack whose frames carry no source 'file' -> no candidate can score onto them
        # (off-stack): amend() returns interesting=False.
        return {"json_dump": {"crash_info": {"crashing_thread": 0},
                              "threads": [{"frames": [
                                  {"function": "Foo::Bar", "line": 10},
                                  {"function": "Baz::Qux", "line": 20},
                              ]}]}}

    def _run(self):
        from crashclouseau import inspector
        return inspector.get_crash_info(
            self._data(), "u-1", _dt(12), "nightly", _dt(9), "buildnode",
            lambda *a, **k: {}, set())

    def test_offstack_stack_dropped_when_disabled(self):
        with mock.patch.object(config, "get_agent_offstack",
                               return_value=_offstack_cfg(enabled=False)):
            self.assertEqual(self._run(), {})   # not stored -> build_seed never sees it

    def test_offstack_stack_stored_when_enabled(self):
        with mock.patch.object(config, "get_agent_offstack",
                               return_value=_offstack_cfg(enabled=True)):
            res = self._run()
        self.assertIn("nonjava", res)                 # stored -> put_report will enqueue it
        self.assertTrue(res["nonjava"]["offstack"])    # flagged no-scored-changeset
        self.assertEqual(len(res["nonjava"]["frames"]), 2)


class TestPrefFlip(unittest.TestCase):
    """Feature/pref-flip detection + off-stack ranking boost + candidate tag."""

    def test_detects_from_desc(self):
        f = orch._looks_pref_flip
        self.assertTrue(f("Bug 2053724 - Enable Rust storage by default", None))  # real bug 2056116
        self.assertTrue(f("Turn on the new layout engine by default", None))
        self.assertTrue(f("flip pref network.foo to true", None))
        self.assertTrue(f("Ship WebGPU by default", None))
        self.assertFalse(f("Fix null deref in nsFoo::Bar", None))     # ordinary fix
        self.assertFalse(f("Refactor the parser", None))

    def test_detects_from_files(self):
        f = orch._looks_pref_flip
        self.assertTrue(f("update", ["modules/libpref/init/StaticPrefList_dom.yaml"]))
        self.assertTrue(f("add feature", ["toolkit/components/nimbus/FeatureManifest.yaml"]))
        self.assertFalse(f("tweak", ["dom/base/nsINode.cpp"]))
        self.assertFalse(f("", []))

    def test_ranking_boost_and_tag(self):
        # a feature-flip floats above a plain candidate and is tagged pref_flip.
        window = [
            {"node": "plain", "bug": 10, "date": _dt(10), "backedout": False, "merge": False,
             "desc": "Bug 10 - unrelated telemetry probe", "files": []},
            {"node": "flip", "bug": 20, "date": _dt(1), "backedout": False, "merge": False,
             "desc": "Bug 20 - Enable Rust storage by default", "files": []},
        ]
        ui = {"signature": "sync15 rust sqlite", "channel": "nightly", "product": "F",
              "buildid": _dt(12), "node": "bn"}
        with mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=window):
            cands = orch._offstack_candidates(ui, _offstack_cfg())
        self.assertEqual(cands[0]["node"], "flip")     # pref-flip ranked above plain
        self.assertTrue(cands[0]["pref_flip"])
        self.assertFalse(cands[1]["pref_flip"])

    def test_user_prompt_tags_and_rule(self):
        from crashclouseau.agent import triage
        crash = {"uuid": "u", "signature": "sync15", "channel": "nightly", "stack": "s",
                 "is_offstack": True,
                 "candidates": [{"node": "flip", "score": None, "bug": 20, "backedout": False,
                                 "pushdate": None, "noise": False, "desc": "Enable Rust storage by default",
                                 "prior_sig": False, "pref_flip": True}]}
        out = triage._user_prompt(crash)
        self.assertIn("LINKED-CAUSE", out)             # the search rule
        self.assertIn("2056116", out)                  # the canonical example
        self.assertIn("feature-flip", out)             # per-candidate tag


class TestPriorSig(unittest.TestCase):
    """The prior-signature (P4) lookup + corroboration gate + ranking."""

    def _fake_bugs(self):
        return [
            {"id": 100, "resolution": "FIXED", "regressed_by": [2011326], "summary": "sib A"},
            {"id": 200, "resolution": "WONTFIX", "regressed_by": [777], "summary": "sib B"},  # not FIXED
            {"id": 300, "resolution": "FIXED", "regressed_by": [555], "summary": "sib C"},
        ]

    def _patch_lookup(self, sig_to_ids):
        from crashclouseau import priorsig as P
        fake = self._fake_bugs()

        class FakeBZ:
            def __init__(self, bugids=None, include_fields=None, bughandler=None, bugdata=None):
                for b in fake:
                    if str(b["id"]) in (bugids or []):
                        bughandler(b, bugdata)

            def get_data(self):
                return self

            def wait(self):
                return self

        return (mock.patch.object(P.SocorroBugs, "get_bugs", return_value=sig_to_ids),
                mock.patch.object(P, "Bugzilla", FakeBZ))

    def test_hints_from_fixed_siblings(self):
        from crashclouseau import priorsig as P
        s, b = self._patch_lookup({"SIG": [100, 200, 300]})
        with s, b:
            hints = P.prior_regressor_hints(["SIG"])
        regs = sorted(h["regressor_bug"] for h in hints)
        self.assertEqual(regs, [555, 2011326])          # 200 (WONTFIX) excluded
        self.assertEqual({h["regressor_bug"]: h["prior_bug"] for h in hints}[2011326], 100)

    def test_exclude_bug_drops_self(self):
        from crashclouseau import priorsig as P
        s, b = self._patch_lookup({"SIG": [100, 300]})
        with s, b:
            hints = P.prior_regressor_hints(["SIG"], exclude_bug=300)
        self.assertEqual([h["regressor_bug"] for h in hints], [2011326])   # 300 excluded

    def test_empty_and_failure_are_graceful(self):
        from crashclouseau import priorsig as P
        self.assertEqual(P.prior_regressor_hints([]), [])
        with mock.patch.object(P.SocorroBugs, "get_bugs", side_effect=RuntimeError("boom")):
            self.assertEqual(P.prior_regressor_hints(["SIG"]), [])

    def test_corroboration_flag_and_bump_single_in_window_prior(self):
        # seed['prior_regressor_bugs'] is already window-intersected in build_seed; a SINGLE
        # in-window prior matching the verdict candidate -> bump.
        d = Dossier(candidate=Candidate(node="n1", bug=2011326),
                    verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                    needinfo_draft="?"))
        seed = {"raw_crash": {}, "prior_regressor_bugs": [2011326]}
        flags = orch._corroborations(d, seed)
        self.assertTrue(flags["prior_signature_match"])
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.probable)   # bare lead -> probable
        self.assertTrue(d.corroborations["prior_signature_match"])

    def test_no_bump_when_ambiguous_multiple_in_window_priors(self):
        # FOCUS GUARD (review): >1 in-window prior = ambiguous hot signature -> no bump,
        # even though the candidate matches one of them.
        d = Dossier(candidate=Candidate(node="n1", bug=2011326),
                    verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                    needinfo_draft="?"))
        seed = {"raw_crash": {}, "prior_regressor_bugs": [2011326, 999]}
        flags = orch._corroborations(d, seed)
        self.assertNotIn("prior_signature_match", flags)
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.medium)     # ambiguous -> no bump

    def test_no_bump_when_candidate_not_a_prior(self):
        d = Dossier(candidate=Candidate(node="n1", bug=555),
                    verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                    needinfo_draft="?"))
        orch._apply_corroboration_gate(d, {"raw_crash": {}, "prior_regressor_bugs": [2011326]})
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_ranking_boosts_prior_sig_candidate(self):
        window = [
            {"node": "a", "bug": 100, "date": _dt(10), "backedout": False, "merge": False, "desc": "x"},
            {"node": "b", "bug": 2011326, "date": _dt(1), "backedout": False, "merge": False, "desc": "y"},
        ]
        ui = {"signature": "S", "channel": "nightly", "product": "F", "buildid": _dt(12), "node": "bn"}
        with mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=window):
            cands = orch._offstack_candidates(ui, _offstack_cfg(), prior_bugs={2011326})
        self.assertEqual(cands[0]["node"], "b")       # prior-sig hit ranked first
        self.assertTrue(cands[0]["prior_sig"])
        self.assertFalse(cands[1]["prior_sig"])

    def test_build_seed_attaches_prior_hints(self):
        res = {"frames": [{"stackpos": 0, "function": "F", "filename": "a.cpp",
                           "line": 1, "changesets": {}}]}
        ui = {"uuid": "u-1", "id": 1, "signature": "S", "buildid": _dt(12),
              "channel": "nightly", "product": "Firefox", "java": False, "node": "bn"}
        window = [{"node": "w1", "bug": 2011326, "date": _dt(3), "backedout": False,
                   "merge": False, "desc": "d"}]
        with mock.patch.object(orch.config, "get_agent_offstack", return_value=_offstack_cfg()), \
             mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, ui)), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"signature": "S", "channel": "nightly"}), \
             mock.patch.object(orch.models.Build, "get_two_last",
                               return_value=[{"revision": "r0"}, {"revision": "r1"}]), \
             mock.patch("crashclouseau.pushlog.pushlog_for_revs", return_value=window), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.priorsig.prior_regressor_hints",
                        return_value=[{"regressor_bug": 2011326, "prior_bug": 900, "prior_summary": "x"},
                                      {"regressor_bug": 8888, "prior_bug": 901, "prior_summary": "y"}]), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}), \
             mock.patch("crashclouseau.inspector.git2hg", return_value="hgbuildnode"):
            seed = orch.build_seed("u-1")
        # 2011326 is a window candidate -> kept; 8888 is NOT in the window (dangling
        # prior) -> dropped from both the corroboration set and the surfaced hints.
        self.assertEqual(seed["prior_regressor_bugs"], [2011326])
        self.assertEqual([h["regressor_bug"] for h in seed["prior_hints"]], [2011326])
        self.assertTrue(seed["candidates"][0]["prior_sig"])          # window cand tagged

    def test_user_prompt_surfaces_prior_hint(self):
        from crashclouseau.agent import triage
        crash = {"uuid": "u", "signature": "S", "channel": "nightly", "stack": "s",
                 "is_offstack": True,
                 "prior_hints": [{"regressor_bug": 2011326, "prior_bug": 900}],
                 "candidates": [{"node": "w1", "score": None, "bug": 2011326, "backedout": False,
                                 "pushdate": None, "noise": False, "desc": "d", "prior_sig": True}]}
        out = triage._user_prompt(crash)
        self.assertIn("PRIOR-SIGNATURE PRIOR", out)
        self.assertIn("bug 2011326", out)
        self.assertIn("prior-sig", out)                              # per-candidate tag


class TestRunEvidenceAgentOffstack(unittest.TestCase):
    """End-to-end wiring of the off-stack gates + reconcile + observe-only through
    run_evidence_agent (run_crash_triage mocked; no SDK/DB/Redis)."""

    def _seed(self):
        return {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1",
                "candidates": [{"node": "w1", "score": None, "bug": 42, "backedout": False,
                                "pushdate": None, "noise": False, "desc": "touch X"}],
                "experts": [], "raw_crash": {}, "is_offstack": True,
                "build_node": "bn", "pin_rev": "bn"}

    def _result(self, callpath_verified, needinfo="ASSERTIVE: changeset X caused this"):
        edges = [_SF_EDGE] if callpath_verified else [_DIFF_EDGE]
        d = Dossier(
            candidate=Candidate(node="abc123def456", bug=42),
            call_path=CallPath(edges=edges),
            verdict=Verdict(decision=Decision.strong_evidence, confidence=Confidence.high,
                            mechanism=Claim(statement="m", citations=[_SF]),
                            consistency=Claim(statement="c", citations=[_SF]),
                            needinfo_draft=needinfo),
        )
        act = {"type": "bugzilla.add_comment",
               "params": {"bug_id": 42, "text": needinfo, "is_private": True},
               "reasoning": "auto-drafted from the verdict's needinfo_draft (strong-evidence); "
                            "human-confirmed before apply"}
        return CrashTriageResult(num_turns=5, total_cost_usd=0.3, result="ok",
                                 dossier=d, actions=[act])

    def _run(self, seed, result, offstack_cfg):
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = True
        MVerd = mock.MagicMock()

        async def fake(**kw):
            return result

        with mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.config, "get_agent_offstack", return_value=offstack_cfg), \
             mock.patch.object(orch.models, "Dossier", MDoss), \
             mock.patch.object(orch.models, "Verdict", MVerd), \
             mock.patch.object(orch.models, "commit"), \
             mock.patch.object(orch, "build_seed", return_value=seed), \
             mock.patch.object(orch, "_seed_score", return_value=5), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", fake):
            orch.run_evidence_agent("u-1")
        done = next((c for c in MDoss.upsert.call_args_list
                     if c.kwargs.get("status") == "done"), None)
        return done, MVerd

    def test_observe_only_suppresses_actions_but_keeps_verdict(self):
        done, MVerd = self._run(self._seed(), self._result(callpath_verified=True),
                                _offstack_cfg(observe_only=True))
        self.assertIsNotNone(done)
        payload = done.kwargs["payload"]
        self.assertEqual(payload["actions"], [])                      # observe-only: nothing to apply
        corr = payload["dossier"]["corroborations"]
        self.assertTrue(corr["offstack_observe_only"])
        self.assertTrue(corr["call_path_verified"])                   # SF-3 flag survived the merge
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "culprit")  # verdict still logged

    def test_graduation_downgraded_lead_ships_soft_draft(self):
        # observe_only OFF + no verified call path: SF-3 downgrades strong->lead, and the
        # persisted apply-eligible action must carry the SOFT draft, not the assertive one.
        done, MVerd = self._run(self._seed(), self._result(callpath_verified=False),
                                _offstack_cfg(observe_only=False))
        self.assertIsNotNone(done)
        payload = done.kwargs["payload"]
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "lead")   # downgraded
        self.assertEqual(len(payload["actions"]), 1)                      # not cleared (observe off)
        text = payload["actions"][0]["params"]["text"]
        self.assertNotIn("ASSERTIVE", text)                               # stale draft gone
        self.assertIn("not an accusation", text)                          # soft draft shipped

    def _seed_onstack(self):
        s = self._seed()
        s["is_offstack"] = False
        s["raw_crash"] = {"json_dump": {"crash_info": {"address": "0xe5e5e5e5"}}}  # poison
        return s

    def test_onstack_exposer_runs_but_not_sf3_or_observe_only(self):
        # On-stack (is_offstack False): the exposer classifier runs (poison fault -> downgrade
        # strong->lead) even though SF-3 and observe-only are off-stack-only. The bridged
        # action is reconciled to the SOFT lead draft and stays apply-eligible (on-stack
        # verdicts are not observe-only).
        seed = self._seed_onstack()
        result = self._result(callpath_verified=True)   # strong + call path; exposer downgrades anyway
        done, MVerd = self._run(seed, result, _offstack_cfg())  # offstack_cfg is unused for on-stack
        self.assertIsNotNone(done)
        payload = done.kwargs["payload"]
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "lead")   # exposer downgraded
        corr = payload["dossier"]["corroborations"]
        self.assertTrue(corr["exposer_suspected"])                        # exposer ran on-stack
        self.assertNotIn("offstack_observe_only", corr)                   # on-stack NOT observe-only
        self.assertNotIn("call_path_verified", corr)                      # SF-3 did NOT run on-stack
        self.assertEqual(len(payload["actions"]), 1)                      # apply-eligible (not suppressed)
        self.assertIn("not an accusation", payload["actions"][0]["params"]["text"])  # soft draft


if __name__ == "__main__":
    unittest.main()
