# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The two fixes from crash 0027161c-203a-4bc5-bb1d-efa910260905 (release 155.0.1, 2026-09-06).

`AsyncShutdownTimeout | profile-before-change | CookiePersistentStorage: cookies.sqlite closing`
ran at 1-2 reports/day on 154.x, 5-8/day on 155.0 and 30-35/day on 155.0.1. The 155.0 -> 155.0.1
diff was 22 changesets with exactly one cookie/storage change, bug 2066155 (the WAL cap back down
from 2MB to 512KB, re-exposing the fsync-per-checkpoint cost that bug 1158387's `synchronous =
NORMAL` introduced in 155). The principal FOUND that candidate and wrote a medium lead; the skeptic
failed "consistency (build-timing)" with "the 512KB cap is ALREADY present in this exact crash
build, so the WAL-cap fix cannot be blamed for causing THIS instance", and the veto turned the
lead into a noise abstain.

Two things, tested here:
  * `sigage.version_rates` / `triage._version_rate_lines` -- the per-version rate fact that
    was missing from the prompt (nightly has `sigtrend`; beta and release had nothing).
  * `schema.is_presence_ground` -- a `fail` resting on "already present in the build" no longer
    binds on a lead (rule 1d of `Dossier._skeptic_veto`), and the skeptic prompt says why.

Run: DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
     python -m unittest tests.test_version_step
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, sigage  # noqa: E402
from crashclouseau.agent import orchestrator as orch, roles, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    SkepticResult,
    SkepticStatus,
    Verdict,
    is_presence_ground,
)

# The verbatim prod note that vetoed the correct lead.
_PROD_NOTE = ("Independently confirmed via pinned source read that the 512KB cap (bug 2066155) is "
              "ALREADY present in this exact crash build, so the WAL-cap fix cannot be blamed for "
              "causing THIS instance — it argues the opposite direction. Downgrades this from "
              "a 'cause' claim to an 'incomplete fix / still-open area' lead.")


def _bucket(day, **versions):
    return {"term": day + "T00:00:00+00:00", "count": sum(versions.values()),
            "facets": {"version": [{"term": v, "count": n} for v, n in versions.items()]}}


def _response():
    """The 0027161c signature on release, 2026-09-01..06, as SuperSearch returns it (rounded)."""
    return {
        "total": 100,
        "facets": {
            "histogram_date": [
                _bucket("2026-08-25", **{"154.0.1": 1}),
                _bucket("2026-08-31", **{"154.0.1": 2}),
                _bucket("2026-09-01", **{"154.0.1": 1}),
                _bucket("2026-09-02", **{"155.0": 5}),
                _bucket("2026-09-03", **{"155.0": 8}),
                _bucket("2026-09-04", **{"155.0": 6, "155.0.1": 9}),
                _bucket("2026-09-05", **{"155.0": 4, "155.0.1": 35}),
                _bucket("2026-09-06", **{"155.0": 1, "155.0.1": 30}),
            ],
            "version": [
                {"term": "155.0.1", "count": 74,
                 "facets": {"build_id": [{"term": "20260903215306", "count": 74}]}},
                {"term": "155.0", "count": 24,
                 "facets": {"build_id": [{"term": "20260826195058", "count": 21},
                                         {"term": "20260902090331", "count": 3}]}},
                {"term": "154.0.1", "count": 4, "facets": {"build_id": []}},
            ],
        },
    }


class TestSummarizeVersionRates(unittest.TestCase):
    def test_rows_are_oldest_first_with_per_day_over_the_versions_span(self):
        out = sigage.summarize_version_rates(_response(), days=60)
        self.assertEqual([r["version"] for r in out["versions"]], ["154.0.1", "155.0", "155.0.1"])
        r155 = out["versions"][1]
        self.assertEqual((r155["reports"], r155["first_day"], r155["last_day"], r155["days"]),
                         (24, "2026-09-02", "2026-09-06", 5))
        self.assertAlmostEqual(r155["per_day"], 4.8)
        self.assertEqual(out["versions"][2]["build_ids"], ["20260903215306"])
        self.assertEqual(out["days"], 60)

    def test_the_155_0_1_step_is_found(self):
        step = sigage.summarize_version_rates(_response())["step"]
        self.assertIsNotNone(step)
        self.assertEqual((step["version"], step["from_version"]), ("155.0.1", "155.0"))
        self.assertAlmostEqual(step["per_day"], 74 / 3.0, places=2)
        self.assertGreaterEqual(step["ratio"], 5.0)
        self.assertEqual(step["build_ids"], ["20260903215306"])

    def test_a_version_below_min_reports_is_never_the_comparison_point(self):
        # 154.0.1 has 4 reports: it is listed, but the step compares 155.0.1 against 155.0, not
        # against a version whose "rate" is 1 of 3 being 33%.
        out = sigage.summarize_version_rates(_response(), min_reports=5)
        self.assertIn("154.0.1", [r["version"] for r in out["versions"]])
        self.assertEqual(out["step"]["from_version"], "155.0")

    def test_a_drift_is_not_a_step(self):
        self.assertIsNone(sigage.summarize_version_rates(_response(), step_ratio=6.0)["step"])

    def test_an_empty_response_is_no_versions_and_no_step(self):
        self.assertEqual(sigage.summarize_version_rates({"facets": {}}),
                         {"versions": [], "step": None, "days": sigage.VERSION_RATES_DAYS})
        self.assertEqual(sigage.summarize_version_rates(None)["versions"], [])


class TestVersionRatesLookup(unittest.TestCase):
    def _fake(self, seen, payload):
        class FakeSearch:
            URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

            def __init__(self, params=None, handler=None, handlerdata=None, queries=None, **kw):
                self._h = []
                for q in queries or []:
                    seen.append(dict(q.params or {}))
                    self._h.append((q.handler, q.handlerdata))

            def wait(self):
                for h, d in self._h:
                    if payload is not None:
                        h(payload, d)
        return FakeSearch

    def test_one_query_scoped_to_the_channel_with_a_per_day_version_histogram(self):
        seen = []
        with mock.patch.object(sigage.socorro, "SuperSearch", self._fake(seen, _response())):
            out = sigage.version_rates("sig", channel="release", days=60)
        self.assertEqual(len(seen), 1)
        p = seen[0]
        self.assertEqual(p["signature"], "=sig")
        self.assertEqual(p["release_channel"], sigage.utils.get_search_channel("release"))
        self.assertEqual((p["_histogram.date"], p["_histogram_interval.date"]), ("version", "1d"))
        self.assertEqual(p["_aggs.version"], "build_id")
        self.assertEqual(out["step"]["version"], "155.0.1")

    def test_failure_is_unknown_not_no_change(self):
        with mock.patch.object(sigage.socorro, "SuperSearch", self._fake([], None)):
            self.assertEqual(sigage.version_rates("sig", channel="release"),
                             sigage.NO_VERSION_RATES)
        with mock.patch.object(sigage.socorro, "SuperSearch", side_effect=RuntimeError("down")):
            self.assertEqual(sigage.version_rates("sig", channel="release"),
                             sigage.NO_VERSION_RATES)
        self.assertEqual(sigage.version_rates("", channel="release"), sigage.NO_VERSION_RATES)


class TestVersionRateLines(unittest.TestCase):
    def _crash(self, **over):
        c = {"uuid": "0027161c", "signature": "sig", "channel": "release", "version": "155.0.1",
             "version_rates": sigage.summarize_version_rates(_response())}
        c.update(over)
        return c

    def test_the_block_names_the_step_and_marks_this_crashs_version(self):
        text = "\n".join(triage._version_rate_lines(self._crash()))
        self.assertIn("CRASH RATE BY VERSION on release", text)
        self.assertIn("155.0: 4.8/day over 5 days (24 reports)", text)
        self.assertIn("155.0.1: 24.7/day over 3 days (74 reports)   <- this crash's version", text)
        self.assertIn("STEP: 155.0.1 runs at 5.1x the rate of 155.0", text)
        self.assertIn("build 20260903215306", text)
        # The two sentences the 0027161c skeptic needed.
        self.assertIn("never a refutation", text)
        self.assertIn("mitigation can still be the regressor", text)
        self.assertIn("This report is on the step version", text)

    def test_a_report_on_another_version_is_not_told_its_window_is_the_step(self):
        text = "\n".join(triage._version_rate_lines(self._crash(version="155.0")))
        self.assertIn("155.0: 4.8/day over 5 days (24 reports)   <- this crash's version", text)
        self.assertNotIn("This report is on the step version", text)

    def test_nothing_to_compare_prints_nothing(self):
        self.assertEqual(triage._version_rate_lines(self._crash(version_rates=None)), [])
        self.assertEqual(triage._version_rate_lines(
            self._crash(version_rates=dict(sigage.NO_VERSION_RATES))), [])
        one = {"versions": [{"version": "155.0.1", "reports": 74, "first_day": "2026-09-04",
                             "last_day": "2026-09-06", "days": 3, "per_day": 24.67,
                             "build_ids": []}], "step": None, "days": 60}
        self.assertEqual(triage._version_rate_lines(self._crash(version_rates=one)), [])

    def test_the_block_reaches_the_shared_crash_facts(self):
        # Shared with the blind second opinion, like the trend and hardware blocks.
        crash = self._crash(raw_crash={"json_dump": {}})
        text = "\n".join(triage._crash_facts(crash))
        self.assertIn("CRASH RATE BY VERSION", text)


class TestSeedWiring(unittest.TestCase):
    def test_off_switch_returns_the_unknown_shape_without_a_query(self):
        with mock.patch.object(config, "get_agent_version_rates",
                               return_value={"enabled": False, "days": 60, "step_ratio": 3.0,
                                             "min_reports": 5}), \
             mock.patch.object(sigage, "version_rates") as vr:
            self.assertEqual(orch._version_rates({"signature": "s"}, "release"),
                             sigage.NO_VERSION_RATES)
        vr.assert_not_called()

    def test_the_knobs_reach_the_lookup(self):
        with mock.patch.object(config, "get_agent_version_rates",
                               return_value={"enabled": True, "days": 30, "step_ratio": 2.5,
                                             "min_reports": 7}), \
             mock.patch.object(sigage, "version_rates", return_value={"versions": [], "step": None,
                                                                      "days": 30}) as vr:
            orch._version_rates({"signature": "s", "product": "Firefox"}, "beta")
        vr.assert_called_once_with("s", product="Firefox", channel="beta", days=30,
                                   step_ratio=2.5, min_reports=7)

    def test_record_version_step_flags_the_step_and_whether_this_crash_is_on_it(self):
        d = Dossier(crash={"uuid": "u", "signature": "sig", "frames": []})
        seed = {"version": "155.0.1", "version_rates": sigage.summarize_version_rates(_response())}
        orch._record_version_step(d, seed)
        self.assertEqual(d.corroborations["version_step"], "155.0.1 at 5.1x the rate of 155.0")
        self.assertAlmostEqual(d.corroborations["version_step_ratio"], 5.1)
        self.assertTrue(d.corroborations["crash_in_step_version"])
        d2 = Dossier(crash={"uuid": "u", "signature": "sig", "frames": []})
        orch._record_version_step(d2, {**seed, "version": "155.0"})
        self.assertFalse(d2.corroborations["crash_in_step_version"])

    def test_record_version_step_is_a_no_op_without_a_step(self):
        d = Dossier(crash={"uuid": "u", "signature": "sig", "frames": []})
        orch._record_version_step(d, {"version_rates": dict(sigage.NO_VERSION_RATES)})
        orch._record_version_step(d, {})
        self.assertNotIn("version_step", d.corroborations or {})


# ---------------------------------------------------------------------------------------------
# The presence inversion.

_SF = SearchfoxCitation(permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-release")


def _lead_with(*notes, decision=Decision.lead):
    extra = {"confidence": Confidence.probable}
    if decision is Decision.strong_evidence:
        extra = {"confidence": Confidence.high,
                 "consistency": Claim(summary="only candidate in the window", citations=[_SF])}
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(decision=decision,
                        mechanism=Claim(summary="WAL checkpoint fsync on close", citations=[_SF]),
                        **extra),
        candidate=Candidate(node="ab9673b0a48d", bug=2066155, author="A", channel="release"),
        skeptic=[SkepticResult(status=SkepticStatus.failed, claim_ref="claim{}".format(i),
                               note=n, citations=[]) for i, n in enumerate(notes)],
    )


class TestIsPresenceGround(unittest.TestCase):
    def test_the_prod_note_is_a_presence_ground(self):
        self.assertTrue(is_presence_ground(_PROD_NOTE))

    def test_other_ways_of_saying_it(self):
        for note in (
            "the candidate is already in this build, so it cannot be the cause",
            "This crash build already contains bug 2066155's patch.",
            "the fix is already present in 155.0.1, which argues the opposite direction",
            "Since the patch shipped in this build it could not have caused this crash",
        ):
            with self.subTest(note=note):
                self.assertTrue(is_presence_ground(note))

    def test_a_real_timing_refutation_keeps_its_teeth(self):
        # Absence IS the build-timing refutation. Every one of these must still bind.
        for note in (
            "the candidate landed after this build (2026-09-04 vs build 2026-09-03)",
            "bug 2066155 is not present in this build; it was uplifted the day after",
            "this changeset was backed out before the build, so the code is not in the binary",
            "the patch predates the signature by three years and is already in every build",
            "changeset is absent from the 155.0.1 tag",
        ):
            with self.subTest(note=note):
                self.assertFalse(is_presence_ground(note))

    def test_an_ordinary_contradiction_is_not_a_presence_ground(self):
        for note in (
            "the cited diff line 2165 is not present in changeset ff789e9f149e",
            "GTK-gated Linux ibus/fcitx key-event plumbing, not compiled into Windows builds",
            "calls_to shows no edge from the crashing frame to the changed function",
            "",
            None,
        ):
            with self.subTest(note=note):
                self.assertFalse(is_presence_ground(note))


class TestWhatAPresenceFailMayDo(unittest.TestCase):
    """Rule (1d) of `Dossier._skeptic_veto`."""

    def test_the_0027161c_veto_no_longer_abstains_the_lead(self):
        d = _lead_with(_PROD_NOTE)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["skeptic_presence_unbound"], ["claim0"])
        self.assertNotIn("skeptic_build_flag_unbound", d.corroborations)

    def test_an_ordinary_fail_still_abstains_a_lead(self):
        d = _lead_with("the cited diff line 2165 is not present in changeset ff789e9f149e")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("noise", d.verdict.abstain_reason)

    def test_a_genuine_absence_fail_still_abstains_a_lead(self):
        d = _lead_with("bug 2066155 is not present in this build; it landed after it")
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_binding_fail_beside_a_presence_fail_still_binds(self):
        d = _lead_with(_PROD_NOTE, "calls_to shows no edge from the crashing frame to the change")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("claim1", d.verdict.abstain_reason)
        self.assertNotIn("claim0", d.verdict.abstain_reason)

    def test_strong_evidence_is_still_downgraded_to_a_lead_by_any_fail(self):
        # Rule (1) is untouched: a fail of any ground costs strong-evidence its status.
        d = _lead_with(_PROD_NOTE, decision=Decision.strong_evidence)
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestTheSkepticIsToldWhy(unittest.TestCase):
    def test_the_prompt_carries_the_presence_rule(self):
        prompt = roles._ROLES["skeptic"]["prompt"]
        self.assertIn("never a refutation", prompt)
        self.assertIn("ABSENT from the build", prompt)
        self.assertIn("mitigation can still be the regressor", prompt)

    def test_the_clause_stays_short(self):
        self.assertLess(len(roles._PRESENCE.split()), 110)


if __name__ == "__main__":
    unittest.main()
