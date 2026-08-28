# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""We must not publish an analysis of a memory-safety crash.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_sensitive

On 2026-08-20 Clouseau filed bug 2065051 from a crash whose fault address is mozjemalloc's
``kAllocPoison``; a human made it a security bug, and :mccr8 asked for poison crashes to be filed
as security issues from the start. Restricting the Bugzilla bug would not have contained it: four
days later ``GET /crashstack.html?uuid=41bb8c8a-...`` still answered HTTP 200 anonymously with
``0xe5e5e5e5e5e5e5e8``, "Use-after-free" and the suspected regressor, while BMO answered 102.

The test that matters most in this file is `test_every_route_that_can_reach_a_dossier_is_gated`.
The predicate is easy; remembering it exists when a fifth surface is added is the hard part.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import api, bugzilla_apply, sensitive                  # noqa: E402

# The real shape of the crash behind bug 2065051: `crash_info.address` is PRESENT and useless,
# and the poison value is in Socorro's normalized top-level field. This is the shape that made
# `exposer_strong` fail-open on 6 of 8 measured poison crashes.
_POISON_RAW = {"json_dump": {"crash_info": {"address": "0x0000000000000000"}},
               "address": "0xe5e5e5e5e5e5e5e8"}


class TestThePredicate(unittest.TestCase):
    def test_the_2065051_shape_fires_even_though_crash_info_says_null(self):
        signals = sensitive.memory_unsafe_signals(_POISON_RAW)
        self.assertTrue(signals)
        self.assertIn("0xe5e5e5e5e5e5e5e8", signals[0])

    def test_a_plain_null_deref_does_not_fire(self):
        self.assertEqual(sensitive.memory_unsafe_signals(
            {"json_dump": {"crash_info": {"address": "0x0"}}, "address": "0x0"}), [])

    def test_the_hardware_bit_flip_does_not_fire(self):
        """Bug 2064600 reads `failure_class == "uaf"` and its real fault address is
        0xffffffffffffffff -- tnikkel diagnosed a hardware bit flip. It is the measured
        counter-example for gating on the MODEL's label: that union fires on 43 of 500 runs and
        on 2 of 11 new-bug filings, and every human who read those bugs left them public."""
        for addr in ("0xffffffffffffffff", "0xffffffff"):
            with self.subTest(addr=addr):
                self.assertEqual(sensitive.memory_unsafe_signals(
                    {"json_dump": {"crash_info": {"address": addr}}, "address": addr}), [])

    def test_an_offset_into_a_poisoned_object_fires_on_the_prefix_rule(self):
        # The 13.7% the dominance rule drops: the low bytes are the offset, not the fill.
        signals = sensitive.memory_unsafe_signals({"address": "0xe5e5e5e5e5e50128"})
        self.assertTrue(signals)
        self.assertIn("poison prefix", signals[0])

    def test_a_two_byte_poison_address_fires_matching_the_gates_own_rule(self):
        """0xe5e5 is above `_MAX_FIELD_FAULT` and both its bytes are poison, so the dominance
        rule accepts it -- and that is deliberate upstream: the orchestrator's docstring records
        that across 89 days exactly ONE two-byte address in the census has the 0xXYXY shape, and
        it is 0xA4A4, a byte in no version of the set. Pinned here so the two rules cannot drift
        apart silently, not because this shape matters on its own."""
        self.assertTrue(sensitive.memory_unsafe_signals({"address": "0xe5e5"}))
        self.assertEqual(sensitive.memory_unsafe_signals({"address": "0xe512"}), [])

    def test_an_address_inside_the_first_page_is_left_to_the_field_offset_corroboration(self):
        self.assertEqual(sensitive.memory_unsafe_signals({"address": "0x1000"}), [])

    def test_no_address_at_all_does_not_fire(self):
        # A MOZ_CRASH or a hang dereferenced nothing, so absence is not evidence. 2.42% of
        # nightly reports have no parseable address.
        for raw in (None, {}, {"json_dump": {}}, {"address": ""}, {"address": "not-hex"}):
            with self.subTest(raw=raw):
                self.assertEqual(sensitive.memory_unsafe_signals(raw), [])

    def test_there_is_only_one_byte_set_and_one_rule(self):
        """`sensitive` OWNS both; `agent.orchestrator` aliases them. Identity, not equality --
        two equal copies can drift and this cannot. The import direction is forced: `sensitive`
        must stay dependency-free because importing `agent.orchestrator` opens a Redis connection
        at module scope, and this module is imported on every web request."""
        from crashclouseau.agent import orchestrator
        self.assertIs(orchestrator._POISON_BYTES, sensitive.POISON_BYTES)
        self.assertIs(orchestrator._looks_poison, sensitive.looks_poison)

    def test_the_gate_now_sees_the_widened_rule_too(self):
        """The point of unifying: the prefix half reaches `build_exposer_note`'s published
        sentence, not just the withholding decision. 4 of the 12 poison faults in the 500-run
        prod panel need it."""
        from crashclouseau.agent import orchestrator
        for addr in (0xE5E5E5E5E5E5E604, 0xE5E5E5E5E5E5E60D, 0xE5E5E5E5E5E6022D):
            with self.subTest(addr=hex(addr)):
                self.assertFalse(orchestrator._looks_poison_dominant_only(addr))
                self.assertTrue(orchestrator._looks_poison(addr))
        # ...and the two rules still agree on everything the dominance rule already caught.
        for addr in (0xE5E5E5E5E5E5E5E8, 0xE5E5E5E5E5E5E5E5, 0xCCCCCCCC, 0x0, 0x1000,
                     0xE512, 0xA4A4, 0xDEADBEEF, 0xFFFFFFFFFFFFFFFF, 0x7FFF12345678):
            with self.subTest(addr=hex(addr)):
                if orchestrator._looks_poison_dominant_only(addr):
                    self.assertTrue(orchestrator._looks_poison(addr))

    def test_the_promotion_gates_keep_the_OLD_address_reader(self):
        """`_fault_address` is deliberately NOT fixed. Its other callers act only on
        `0 < fault <= MAX_FIELD_FAULT`, and the old value in exactly the cases the fix changes
        (0x0, all-ones) can never satisfy that window -- so today they always skip. Feeding them
        Socorro's address could make a PROMOTION gate newly fire on up to 8% of runs, and that
        cannot be measured offline because `crash_info.address` is not persisted."""
        from crashclouseau.agent import orchestrator
        raw = {"json_dump": {"crash_info": {"address": "0x0"}}, "address": "0x28"}
        self.assertEqual(orchestrator._fault_address(raw), 0)      # unchanged: skips the window
        self.assertEqual(sensitive.fault_address(raw), 0x28)       # the corrected read

    def test_is_withheld_reads_the_persisted_flag_only(self):
        self.assertTrue(sensitive.is_withheld({"memory_unsafe": True}))
        for c in (None, {}, {"memory_unsafe": False}, {"exposer_strong": True},
                  {"failure_class": "uaf"}):
            with self.subTest(c=c):
                self.assertFalse(sensitive.is_withheld(c))


class TestTheRecorder(unittest.TestCase):
    def _dossier(self):
        return mock.MagicMock(corroborations=None)

    def test_it_records_the_flag_and_the_reason(self):
        from crashclouseau.agent import orchestrator
        d = self._dossier()
        orchestrator._record_sensitivity(d, {"raw_crash": _POISON_RAW, "uuid": "u-1"})
        self.assertTrue(d.corroborations["memory_unsafe"])
        self.assertTrue(d.corroborations["memory_unsafe_signals"])

    def test_it_writes_nothing_for_an_ordinary_crash(self):
        from crashclouseau.agent import orchestrator
        d = self._dossier()
        orchestrator._record_sensitivity(d, {"raw_crash": {"address": "0x0"}, "uuid": "u-1"})
        self.assertIsNone(d.corroborations)

    def test_it_fails_SENSITIVE_when_the_check_itself_breaks(self):
        """Inverts this codebase's usual swallow-and-continue. A withheld page costs a click; a
        published use-after-free cannot be taken back."""
        from crashclouseau.agent import orchestrator
        d = self._dossier()
        with mock.patch.object(sensitive, "memory_unsafe_signals",
                               side_effect=RuntimeError("boom")):
            orchestrator._record_sensitivity(d, {"raw_crash": _POISON_RAW, "uuid": "u-1"})
        self.assertTrue(d.corroborations["memory_unsafe"])

    def test_it_moves_no_rung_and_blocks_no_filing(self):
        """A RECORDER. If this ever starts touching the verdict it has become a gate, and the
        filing consequences (which are item 2, not this) would ride along unmeasured."""
        from crashclouseau.agent import orchestrator
        import inspect
        src = inspect.getsource(orchestrator._record_sensitivity)
        for forbidden in ("verdict", "confidence", "Decision", "decision"):
            self.assertNotIn(forbidden, src, forbidden)


class TestTheChokepoint(unittest.TestCase):
    EV = {"uuid": "u-1", "status": "done", "verdict": "culprit", "confidence": 85,
          "rationale": "a stale, already-freed RefPtr<nsAtom> mLang",
          "actions": [], "dossier": {"corroborations": {"memory_unsafe": True,
                                                        "memory_unsafe_signals": ["poison"]},
                                     "verdict": {"mechanism": {"statement": "UAF"}},
                                     "candidate": {"node": "90d0043f91a1"}}}

    def _build(self, **kw):
        with mock.patch.object(bugzilla_apply.models.Verdict, "get_evidence",
                               return_value=dict(self.EV)):
            return bugzilla_apply.build_evidence("u-1", **kw)

    def test_public_is_the_default_so_a_new_caller_is_safe(self):
        import inspect
        sig = inspect.signature(bugzilla_apply.build_evidence)
        self.assertIs(sig.parameters["public"].default, True)

    def test_a_withheld_dossier_leaks_nothing(self):
        ev = self._build()
        self.assertTrue(ev["withheld"])
        blob = repr(ev)
        for leak in ("RefPtr", "mLang", "UAF", "90d0043f91a1", "culprit", "85"):
            self.assertNotIn(leak, blob, leak)
        # ... and the run's existence is still reportable.
        self.assertEqual(ev["status"], "done")

    def test_an_authorized_viewer_gets_everything(self):
        ev = self._build(public=False)
        self.assertNotIn("withheld", ev)
        self.assertEqual(ev["verdict"], "culprit")

    def test_an_ordinary_dossier_is_untouched(self):
        ev = dict(self.EV)
        ev["dossier"] = {"corroborations": {}, "verdict": {}}
        with mock.patch.object(bugzilla_apply.models.Verdict, "get_evidence",
                               return_value=ev):
            out = bugzilla_apply.build_evidence("u-1")
        self.assertNotIn("withheld", out)
        self.assertEqual(out["verdict"], "culprit")


class TestTheViewerGate(unittest.TestCase):
    def _ctx(self, headers=None, args="", cookies=None):
        from crashclouseau import app
        return app.test_request_context("/api/evidence?uuid=u-1" + args,
                                        headers=headers or {},
                                        environ_base={"HTTP_COOKIE": cookies or ""})

    def test_unset_token_refuses_rather_than_opens(self):
        """Matches `_require_write_token`: an unset secret must never read as "no authentication
        required". Here that makes withheld analyses unreachable, not public."""
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": ""}, clear=False):
            with self._ctx(headers={"X-Clouseau-Token": "anything"}):
                self.assertFalse(api.viewer_authorized())

    def test_the_header_the_query_arg_and_the_cookie_all_work(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}, clear=False):
            with self._ctx(headers={"X-Clouseau-Token": "s3cret"}):
                self.assertTrue(api.viewer_authorized())
            with self._ctx(args="&token=s3cret"):
                self.assertTrue(api.viewer_authorized())
            with self._ctx(cookies="{}=s3cret".format(api.VIEW_COOKIE)):
                self.assertTrue(api.viewer_authorized())

    def test_a_wrong_token_does_not(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}, clear=False):
            for kw in ({"headers": {"X-Clouseau-Token": "nope"}}, {"args": "&token=nope"},
                       {"cookies": "{}=nope".format(api.VIEW_COOKIE)}, {}):
                with self.subTest(kw=kw):
                    with self._ctx(**kw):
                        self.assertFalse(api.viewer_authorized())


class TestTheWithheldPageStillRenders(unittest.TestCase):
    """WITHHOLDING IS ONLY HALF THE JOB: the page has to say so.

    `build_evidence` returns a deliberately partial dict for a withheld dossier, and on
    2026-08-28 the first live poison crash to reach a route -- 2c6e9fcf, fault address
    0xe5e5e5e5e5e5e615, verdict LEAD -- hit `evidence["ui"]` in `html.crashstack` and
    500'd. Nothing leaked, and nothing rendered either: `crashstack.html` already carries an
    "Analysis withheld" banner that no reader ever saw.

    `TestEverySurfaceIsGated` could not catch it because it reads the SOURCE for the gate.
    These drive the routes with the real partial dict instead, which is the only thing that
    distinguishes "withholds" from "withholds and survives"."""

    # The prod shape: a LEAD with a cited mechanism and a named regressor, flagged unsafe.
    EV = {"uuid": "u-1", "status": "done", "verdict": "lead", "confidence": 70,
          "rationale": "a stale, already-freed RefPtr<nsAtom> mLang",
          "principal_model": "claude-opus-4-8", "effort": "high", "evidence": [],
          "over_budget": False, "cost_usd": 2.5,
          "actions": [{"type": "bugzilla.add_comment", "params": {}, "reasoning": "x"}],
          "dossier": {"corroborations": {"memory_unsafe": True,
                                         "memory_unsafe_signals":
                                             ["fault address 0xe5e5e5e5e5e5e615 has a "
                                              "4-byte poison prefix"]},
                      "verdict": {"mechanism": {"statement": "use-after-free of mLang"}},
                      "area_experts": [{"name": "someone", "email": "a@b.c"}],
                      "candidate": {"node": "90d0043f91a1", "bug": 1234}}}
    # Everything the banner must NOT contain, plus the two strings only the panel prints.
    LEAKS = ("RefPtr", "mLang", "use-after-free", "90d0043f91a1", "claude-opus",
             "someone", "a@b.c", "evidence-panel")

    def setUp(self):
        from crashclouseau import app
        self.client = app.test_client()

    def _real_withheld(self):
        """Patch the RAW read, so the withheld dict is the one prod builds, not a fixture."""
        return mock.patch.object(bugzilla_apply.models.Verdict, "get_evidence",
                                 return_value=dict(self.EV))

    def test_crashstack_renders_the_banner_instead_of_500ing(self):
        from crashclouseau import population
        from tests.test_product_wiring import _stack, _uuid_info
        with self._real_withheld(), \
                mock.patch("crashclouseau.models.CrashStack.get_by_uuid",
                           return_value=(_stack(), _uuid_info())), \
                mock.patch.object(population, "for_crash", return_value=None):
            rv = self.client.get("/crashstack.html?uuid=u-1")
        self.assertEqual(rv.status_code, 200)
        body = rv.get_data(as_text=True)
        self.assertIn("Analysis withheld", body)
        # the reason is public (it is the fault address, which crash-stats already shows)...
        self.assertIn("poison prefix", body)
        # ...and nothing the analysis concluded is.
        for leak in self.LEAKS:
            self.assertNotIn(leak, body, leak)

    def test_codeview_renders_the_public_diff_with_no_highlighting(self):
        """Same partial dict, second surface. `node` is omitted so no hg fetch is attempted."""
        with self._real_withheld():
            rv = self.client.get("/codeview.html?uuid=u-1&filename=dom/Foo.cpp&line=42")
        self.assertEqual(rv.status_code, 200)
        for leak in ("90d0043f91a1", "mLang", "use-after-free"):
            self.assertNotIn(leak, rv.get_data(as_text=True), leak)

    def test_the_api_serialises_it(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}, clear=False), \
                self._real_withheld():
            rv = self.client.get("/api/evidence?uuid=u-1")
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["withheld"])
        self.assertIsNone(rv.get_json()["verdict"])


class TestEverySurfaceIsGated(unittest.TestCase):
    """THE TEST THAT HAS TO SURVIVE. The predicate is easy; the failure mode is a fifth surface
    added later by someone who has never read `sensitive.py`."""

    # Every module-level function that can reach a persisted dossier, and how it is gated.
    # `build_evidence` covers three of them through one chokepoint; `_draft_evidence` (bug.html)
    # reads `Verdict.get_evidence` directly and carries its own check.
    GATED = {
        ("crashclouseau.html", "crashstack"): "build_evidence",
        ("crashclouseau.html", "codeview"): "build_evidence",
        ("crashclouseau.html", "_draft_evidence"): "is_withheld",
        ("crashclouseau.api", "evidence"): "build_evidence",
    }

    def test_each_known_surface_is_gated(self):
        import importlib
        import inspect
        for (mod_name, fn_name), needle in self.GATED.items():
            with self.subTest(fn="{}.{}".format(mod_name, fn_name)):
                fn = getattr(importlib.import_module(mod_name), fn_name)
                src = inspect.getsource(fn)
                self.assertIn(needle, src)
                self.assertIn("viewer_authorized", src)

    def test_no_UNGATED_caller_of_get_evidence_has_appeared(self):
        """`Verdict.get_evidence` is the raw read. Everything that calls it either goes through
        `build_evidence` (which withholds) or must appear in GATED with its own check. A new
        call site fails here, which is the point."""
        import glob
        import re
        allowed = {"crashclouseau/bugzilla_apply.py",   # build_evidence, the chokepoint
                   "crashclouseau/html.py"}             # _draft_evidence, gated per GATED
        offenders = []
        for path in glob.glob("crashclouseau/**/*.py", recursive=True):
            if path.replace(os.sep, "/") in allowed:
                continue
            body = open(path, encoding="utf-8").read()
            # `\.` so this matches a CALL and not `models.py`'s own definition.
            if re.search(r"\.get_evidence\s*\(", body):
                offenders.append(path)
        self.assertEqual(offenders, [], "ungated Verdict.get_evidence caller(s)")


if __name__ == "__main__":
    unittest.main()
