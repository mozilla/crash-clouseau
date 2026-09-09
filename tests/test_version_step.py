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


def _totals():
    """EVERY crash report of those versions on release over the same days -- the denominators
    (`version_rates`' second, unfiltered query), real 2026-09 numbers."""
    return {
        "total": 0,
        "facets": {
            "histogram_date": [
                _bucket("2026-08-25", **{"154.0.1": 768}),
                _bucket("2026-08-31", **{"154.0.1": 15542}),
                _bucket("2026-09-01", **{"154.0.1": 14555}),
                _bucket("2026-09-02", **{"155.0": 5275, "154.0.1": 10973}),
                _bucket("2026-09-03", **{"155.0": 8467}),
                _bucket("2026-09-04", **{"155.0": 8844, "155.0.1": 969}),
                _bucket("2026-09-05", **{"155.0": 3089, "155.0.1": 5522}),
                _bucket("2026-09-06", **{"155.0": 1699, "155.0.1": 7293}),
            ],
            "version": [],
        },
    }


def _summary(**kw):
    return sigage.summarize_version_rates(_response(), totals=_totals(), **kw)


def _trainhop_response():
    """Bug 2070489's signature on release, 2026-09-01..09 (real counts): 155.0.1 matched 155.0
    for four days, then a newtab train-hop XPI was deployed on 09-08 at 17:58Z."""
    sig = {"155.0": [12, 39, 45, 37, 20, 8, 18, 14, 19],
           "155.0.1": [0, 0, 0, 6, 20, 26, 45, 204, 449]}
    days = ["2026-09-0{}".format(i) for i in range(1, 10)]
    return {"total": 0, "facets": {
        "histogram_date": [_bucket(d, **{v: n[i] for v, n in sig.items() if n[i]})
                           for i, d in enumerate(days)],
        "version": [{"term": "155.0.1", "count": 750,
                     "facets": {"build_id": [{"term": "20260903215306", "count": 750}]}},
                    {"term": "155.0", "count": 212, "facets": {"build_id": []}}]}}


def _trainhop_totals():
    allv = {"155.0": [854, 5275, 8467, 8844, 3089, 1699, 2435, 1708, 676],
            "155.0.1": [0, 0, 0, 969, 5522, 7293, 10670, 12765, 8804]}
    days = ["2026-09-0{}".format(i) for i in range(1, 10)]
    return {"total": 0, "facets": {
        "histogram_date": [_bucket(d, **{v: n[i] for v, n in allv.items() if n[i]})
                           for i, d in enumerate(days)],
        "version": []}}


class TestSummarizeVersionRates(unittest.TestCase):
    def test_rows_are_oldest_first_with_a_share_of_the_versions_own_reports(self):
        out = _summary(days=60)
        self.assertEqual([r["version"] for r in out["versions"]], ["154.0.1", "155.0", "155.0.1"])
        r155 = out["versions"][1]
        self.assertEqual((r155["reports"], r155["first_day"], r155["last_day"], r155["days"]),
                         (24, "2026-09-02", "2026-09-06", 5))
        self.assertAlmostEqual(r155["per_day"], 4.8)
        # 24 of 27,374 crash reports of 155.0 -> 0.88 per 1000; 74 of 13,784 of 155.0.1 -> 5.37.
        self.assertEqual((r155["all_reports"], r155["share"]), (27374, 0.88))
        self.assertEqual((out["versions"][2]["all_reports"], out["versions"][2]["share"]),
                         (13784, 5.37))
        self.assertEqual(out["versions"][2]["daily"],
                         [["2026-09-04", 9, 969], ["2026-09-05", 35, 5522],
                          ["2026-09-06", 30, 7293]])
        self.assertEqual(out["versions"][2]["build_ids"], ["20260903215306"])
        self.assertEqual((out["days"], out["normalized"]), (60, True))

    def test_the_155_0_1_step_is_found_at_the_version_boundary(self):
        step = _summary()["step"]
        self.assertIsNotNone(step)
        self.assertEqual((step["version"], step["from_version"]), ("155.0.1", "155.0"))
        self.assertEqual((step["share"], step["from_share"]), (5.37, 0.88))
        self.assertAlmostEqual(step["ratio"], 6.1)
        self.assertEqual(step["kind"], "boundary")
        self.assertEqual(step["build_ids"], ["20260903215306"])

    def test_a_version_below_min_reports_is_never_the_comparison_point(self):
        # 154.0.1 has 4 reports: it is listed, but the step compares 155.0.1 against 155.0, not
        # against a version whose "rate" is 1 of 3 being 33%.
        out = _summary(min_reports=5)
        self.assertIn("154.0.1", [r["version"] for r in out["versions"]])
        self.assertEqual(out["step"]["from_version"], "155.0")

    def test_a_drift_is_not_a_step(self):
        self.assertIsNone(_summary(step_ratio=8.0)["step"])

    def test_without_denominators_there_are_counts_and_no_step(self):
        # THE 2070489 RULE. Reports per day per version is a population count while a version
        # replaces its predecessor; with no denominator no step may be read off it.
        out = sigage.summarize_version_rates(_response())
        self.assertFalse(out["normalized"])
        self.assertIsNone(out["step"])
        self.assertIsNone(out["date_event"])
        self.assertEqual([r["version"] for r in out["versions"]], ["154.0.1", "155.0", "155.0.1"])
        self.assertNotIn("share", out["versions"][2])
        self.assertAlmostEqual(out["versions"][2]["per_day"], 74 / 3.0, places=2)

    def test_a_rise_inside_the_versions_life_is_a_date_event_not_a_step(self):
        # Bug 2070489: 155.0.1's share sat at or below 155.0's for four days, then the train-hop
        # deployment of 09-08. At `step_ratio` 3 the overall ratio (2.7x) is not even a step;
        # at 2 it is, and the timing says where it came from.
        out = sigage.summarize_version_rates(_trainhop_response(), totals=_trainhop_totals(),
                                             step_ratio=2.0)
        self.assertIsNone(out["step"])
        event = out["date_event"]
        self.assertEqual((event["version"], event["from_version"], event["day"]),
                         ("155.0.1", "155.0", "2026-09-08"))
        self.assertLess(event["share_before"], event["from_share"])   # 3.97 vs 5.96 per 1000
        self.assertGreater(event["share_after"], 25.0)
        strict = sigage.summarize_version_rates(_trainhop_response(), totals=_trainhop_totals())
        self.assertIsNone(strict["step"])
        self.assertIsNone(strict["date_event"])

    def test_step_timing_pools_the_first_days_and_needs_min_reports_there(self):
        row = {"daily": [["d1", 1, 1000], ["d2", 1, 1000], ["d3", 1, 1000], ["d4", 60, 1000]]}
        # Three early reports at 1 per 1000 do not clear 3x a 0.2 baseline WITH min_reports 5.
        self.assertEqual(sigage.step_timing(row, 0.2, 3.0, 5), ("inside", "d4"))
        self.assertEqual(sigage.step_timing(row, 0.2, 3.0, 1), ("boundary", None))
        # A rise no single day carries (2 per 1000 each day against a 3.0 bar, and under
        # min_reports every day) is located nowhere: "inside", no day.
        spread = {"daily": [["d1", 2, 1000], ["d2", 2, 1000], ["d3", 2, 1000], ["d4", 2, 1000]]}
        self.assertEqual(sigage.step_timing(spread, 1.0, 3.0, 5), ("inside", None))

    def test_an_empty_response_is_no_versions_and_no_step(self):
        self.assertEqual(sigage.summarize_version_rates({"facets": {}}),
                         {"versions": [], "step": None, "date_event": None,
                          "days": sigage.VERSION_RATES_DAYS, "since": None,
                          "normalized": False})
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

    def test_two_queries_the_signatures_and_the_denominators_in_one_round_trip(self):
        seen = []
        with mock.patch.object(sigage.socorro, "SuperSearch", self._fake(seen, _response())):
            out = sigage.version_rates("sig", channel="release", days=60)
        self.assertEqual(len(seen), 2)
        p, d = seen
        self.assertEqual(p["signature"], "=sig")
        self.assertEqual(p["release_channel"], sigage.utils.get_search_channel("release"))
        self.assertEqual((p["_histogram.date"], p["_histogram_interval.date"]), ("version", "1d"))
        self.assertEqual(p["_aggs.version"], "build_id")
        # The denominator query is the same histogram with NO signature: every crash report of
        # the product on the channel, per version per day.
        self.assertNotIn("signature", d)
        self.assertNotIn("_aggs.version", d)
        for k in ("product", "release_channel", "date", "_histogram.date",
                  "_histogram_interval.date"):
            self.assertEqual(d[k], p[k], k)
        self.assertTrue(out["normalized"])
        self.assertEqual(out["since"], p["date"][2:])
        # The fake answers both queries with the SAME payload, so every share is 1000 and
        # nothing steps -- which is the point: a step needs the two to differ.
        self.assertIsNone(out["step"])

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
             "version_rates": _summary()}
        c.update(over)
        return c

    def test_the_block_names_the_step_and_marks_this_crashs_version(self):
        text = "\n".join(triage._version_rate_lines(self._crash()))
        self.assertIn("CRASH RATE BY VERSION on release", text)
        self.assertIn("per 1000 crash reports of the SAME version", text)
        self.assertIn("155.0: 0.88 per 1000 (24 of 27374 reports, 2026-09-02..2026-09-06)", text)
        self.assertIn("155.0.1: 5.37 per 1000 (74 of 13784 reports, 2026-09-04..2026-09-06)"
                      "   <- this crash's version", text)
        # The per-day rows, so a rise inside a version's life is visible to the reader.
        self.assertIn("per day: 09-04: 9/969, 09-05: 35/5522, 09-06: 30/7293", text)
        self.assertIn("STEP AT THE VERSION BOUNDARY: 155.0.1 has run at 6.1x 155.0's share", text)
        self.assertIn("build 20260903215306", text)
        # The two sentences the 0027161c skeptic needed.
        self.assertIn("never a refutation", text)
        self.assertIn("fix, cap or mitigation can still be the one", text)
        self.assertIn("This report is on the step version", text)
        # And the sentence that wrote bug 2070489 is gone: the block says what a step is
        # evidence OF, never which candidate it belongs to.
        self.assertNotIn("explains the step", text)
        self.assertIn("evidence about the VERSION, not about any one candidate", text)

    def test_a_report_on_another_version_is_not_told_its_window_is_the_step(self):
        text = "\n".join(triage._version_rate_lines(self._crash(version="155.0")))
        self.assertIn("155.0: 0.88 per 1000 (24 of 27374 reports, 2026-09-02..2026-09-06)"
                      "   <- this crash's version", text)
        self.assertNotIn("This report is on the step version", text)

    def test_a_date_event_is_named_as_such_and_credits_no_candidate(self):
        rates = sigage.summarize_version_rates(_trainhop_response(), totals=_trainhop_totals(),
                                               step_ratio=2.0)
        text = "\n".join(triage._version_rate_lines(self._crash(version_rates=rates)))
        self.assertIn("DATE EVENT, NOT A BUILD STEP: 155.0.1's share rose", text)
        self.assertIn("from 2026-09-08", text)
        self.assertIn("No changeset in this version's window explains it", text)
        self.assertNotIn("STEP AT THE VERSION BOUNDARY", text)
        self.assertNotIn("This report is on the step version", text)
        # Both versions' per-day rows are printed, so the reader can see the 09-08 jump.
        self.assertIn("09-07: 45/10670, 09-08: 204/12765, 09-09: 449/8804", text)

    def test_only_the_recent_versions_are_listed(self):
        rates = _summary()
        stale = [{"version": "14{}.0".format(i), "reports": 1, "first_day": "2026-08-01",
                  "last_day": "2026-08-01", "days": 1, "per_day": 1.0, "build_ids": [],
                  "all_reports": 1000, "share": 1.0} for i in range(8)]
        rates = {**rates, "versions": stale + rates["versions"]}
        text = "\n".join(triage._version_rate_lines(self._crash(version_rates=rates)))
        self.assertIn("(5 older versions with reports in the window not listed)", text)
        self.assertNotIn("140.0:", text)
        self.assertIn("147.0:", text)
        self.assertIn("155.0.1: 5.37 per 1000", text)
        # The step's versions are always shown, however far back they sit.
        old_step = {**rates, "step": {**rates["step"], "from_version": "140.0"}}
        text = "\n".join(triage._version_rate_lines(self._crash(version_rates=old_step)))
        self.assertIn("140.0:", text)

    def test_no_step_says_the_frequency_claim_has_no_support(self):
        rates = sigage.summarize_version_rates(_trainhop_response(), totals=_trainhop_totals())
        text = "\n".join(triage._version_rate_lines(self._crash(version_rates=rates)))
        self.assertIn("No step: this crash's version is within 3x", text)
        self.assertIn("has no support in these numbers", text)

    def test_counts_without_denominators_are_labelled_counts_and_carry_no_step(self):
        rates = sigage.summarize_version_rates(_response())
        text = "\n".join(triage._version_rate_lines(self._crash(version_rates=rates)))
        self.assertIn("CRASH REPORTS BY VERSION on release", text)
        self.assertIn("COUNTS, not rates", text)
        self.assertIn("No step can be read off these numbers", text)
        self.assertIn("155.0.1: 74 reports over 3 days (2026-09-04..2026-09-06)", text)
        self.assertNotIn("STEP", text)

    def test_nothing_to_compare_prints_nothing(self):
        self.assertEqual(triage._version_rate_lines(self._crash(version_rates=None)), [])
        self.assertEqual(triage._version_rate_lines(
            self._crash(version_rates=dict(sigage.NO_VERSION_RATES))), [])
        one = {"versions": [{"version": "155.0.1", "reports": 74, "first_day": "2026-09-04",
                             "last_day": "2026-09-06", "days": 3, "per_day": 24.67,
                             "build_ids": []}], "step": None, "date_event": None, "days": 60,
               "since": None, "normalized": True}
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
        seed = {"version": "155.0.1", "version_rates": _summary()}
        orch._record_version_step(d, seed)
        self.assertEqual(d.corroborations["version_step"], "155.0.1 at 6.1x the share of 155.0")
        self.assertAlmostEqual(d.corroborations["version_step_ratio"], 6.1)
        self.assertEqual(d.corroborations["version_step_kind"], "boundary")
        self.assertTrue(d.corroborations["crash_in_step_version"])
        self.assertNotIn("version_date_event", d.corroborations)
        d2 = Dossier(crash={"uuid": "u", "signature": "sig", "frames": []})
        orch._record_version_step(d2, {**seed, "version": "155.0"})
        self.assertFalse(d2.corroborations["crash_in_step_version"])

    def test_record_version_step_records_a_date_event_and_no_step(self):
        d = Dossier(crash={"uuid": "u", "signature": "sig", "frames": []})
        rates = sigage.summarize_version_rates(_trainhop_response(), totals=_trainhop_totals(),
                                               step_ratio=2.0)
        orch._record_version_step(d, {"version": "155.0.1", "version_rates": rates})
        self.assertEqual(d.corroborations["version_date_event"],
                         "155.0.1 rose 2.5x over 155.0 on 2026-09-08")
        for key in ("version_step", "version_step_ratio", "crash_in_step_version"):
            self.assertNotIn(key, d.corroborations)

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
        # 2026-09-09: "a rate that stepped up in the version that shipped the change is evidence
        # FOR it" is gone; a frequency claim is checked on its timing and its controls.
        self.assertNotIn("evidence FOR it", prompt)
        self.assertIn("checked on TIMING", prompt)
        self.assertIn("versions and rollouts WITHOUT the change", prompt)

    def test_the_prompt_says_what_a_pass_means(self):
        prompt = roles._ROLES["skeptic"]["prompt"]
        self.assertIn("equally true for an innocent candidate is not a pass", prompt)
        self.assertIn("no link OBSERVED in this report", prompt)

    def test_the_clause_stays_short(self):
        self.assertLess(len(roles._PRESENCE.split()), 110)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------
# The second gate that ate the same lead once the skeptic let it through: the blind second
# opinion refuted bug 2066155 with "a smaller WAL cap means LESS to checkpoint at close", at
# medium confidence, and the fold abstained the medium lead (five release runs, 2026-09-06 21:15).

from crashclouseau.agent.schema import SecondOpinion  # noqa: E402
from crashclouseau.agent import second_opinion  # noqa: E402


def _step_seed(**over):
    seed = {"uuid": "0027161c", "signature": "sig", "channel": "release", "version": "155.0.1",
            "candidates": [{"node": "ab9673b0a48d", "bug": 2066155}, {"node": "c5f2a83e6d14"}],
            "version_rates": _summary()}
    seed.update(over)
    return seed


def _refuted(confidence="medium"):
    return SecondOpinion(mode="verify", corroborates=False, confidence=confidence,
                         mechanism="close-time checkpoint",
                         refutation="a smaller WAL cap means less to checkpoint at close")


def _medium_lead(node="ab9673b0a48d"):
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                        needinfo_draft="could you take a look?",
                        mechanism=Claim(summary="WAL checkpoint fsync", citations=[_SF])),
        candidate=Candidate(node=node, bug=2066155, author="A", channel="release"),
    )


class TestAStepTiedLeadSurvivesTheBlindReviewer(unittest.TestCase):
    def test_the_0027161c_refutation_now_clamps_instead_of_abstaining(self):
        d = _medium_lead()
        seed = _step_seed()
        orch._record_version_step(d, seed)
        orch._fold_second_opinion(d, _refuted(), seed)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.low)
        self.assertTrue(d.corroborations["second_opinion_refuted"])
        self.assertTrue(d.corroborations["second_opinion_refuted_step_kept"])
        self.assertNotIn("second_opinion_abstained", d.corroborations)
        self.assertIsNotNone(d.verdict.needinfo_draft)

    def test_without_the_step_the_refutation_still_abstains(self):
        d = _medium_lead()
        seed = _step_seed(version_rates=dict(sigage.NO_VERSION_RATES))
        orch._record_version_step(d, seed)
        orch._fold_second_opinion(d, _refuted(), seed)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["second_opinion_abstained"])

    def test_a_report_on_the_previous_version_is_not_step_tied(self):
        d = _medium_lead()
        seed = _step_seed(version="155.0")
        orch._record_version_step(d, seed)
        orch._fold_second_opinion(d, _refuted(), seed)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_candidate_outside_the_window_is_not_step_tied(self):
        d = _medium_lead(node="5b70f67df703")     # bug 1158387, seven weeks before the window
        seed = _step_seed()
        orch._record_version_step(d, seed)
        orch._fold_second_opinion(d, _refuted(), seed)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_no_window_to_consult_fails_closed(self):
        d = _medium_lead()
        seed = _step_seed(candidates=[])
        orch._record_version_step(d, seed)
        orch._fold_second_opinion(d, _refuted(), seed)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_high_confidence_refutation_is_treated_the_same(self):
        # The step is evidence the reviewer never weighed; its own confidence does not change that.
        d = _medium_lead()
        seed = _step_seed()
        orch._record_version_step(d, seed)
        orch._fold_second_opinion(d, _refuted("high"), seed)
        self.assertEqual((d.verdict.decision, d.verdict.confidence), (Decision.lead, Confidence.low))

    def test_membership_helper_matches_the_recorder(self):
        d = _medium_lead()
        seed = _step_seed()
        self.assertTrue(orch._candidate_window_membership(d, seed))
        orch._record_window_membership(d, seed)
        self.assertTrue(d.corroborations["candidate_in_pushlog_window"])
        self.assertIsNone(orch._candidate_window_membership(d, {"candidates": []}))


class TestTheBlindReviewerIsToldAboutTheStep(unittest.TestCase):
    def test_the_system_prompt_weighs_the_step_over_a_direction_intuition(self):
        self.assertIn("CRASH RATE BY VERSION", second_opinion._SYSTEM)
        self.assertIn("a smaller cap means less work", second_opinion._SYSTEM)
        self.assertIn("corroborates: null", second_opinion._SYSTEM)

    def test_the_user_prompt_names_the_step_for_a_report_on_the_step_version(self):
        crash = _step_seed(raw_crash={"json_dump": {}})
        text = second_opinion._user_prompt(crash, {"node": "ab9673b0a48d", "bug": 2066155})
        self.assertIn("CRASH RATE BY VERSION on release", text)          # the shared facts
        self.assertIn("this report is on 155.0.1, whose share of crash reports has run at 6.1x "
                      "155.0's since its first days", text)
        self.assertIn("say whether THIS change is the part of it", text)

    def test_a_date_event_is_told_not_to_credit_the_candidate(self):
        rates = sigage.summarize_version_rates(_trainhop_response(), totals=_trainhop_totals(),
                                               step_ratio=2.0)
        crash = _step_seed(version_rates=rates, raw_crash={"json_dump": {}})
        text = second_opinion._user_prompt(crash, {"node": "c5f2a83e6d14", "bug": 2067488})
        self.assertIn("whose share rose only on 2026-09-08", text)
        self.assertIn("do not credit the candidate with it", text)
        self.assertNotIn("has run at", text)

    def test_the_system_prompt_no_longer_says_a_step_belongs_to_a_window_candidate(self):
        # The sentences that made two 2026-09-09 filings: "that empirical step outweighs" and
        # "refute only when the change cannot touch the awaited path at all".
        self.assertNotIn("empirical step outweighs", second_opinion._SYSTEM)
        self.assertNotIn("cannot touch the awaited path at all", second_opinion._SYSTEM)
        self.assertIn("AT THE VERSION BOUNDARY", second_opinion._SYSTEM)
        self.assertIn("DATE EVENT", second_opinion._SYSTEM)
        self.assertIn("under a rollout that had already deployed the same value",
                      second_opinion._SYSTEM)

    def test_no_note_off_the_step_version_or_without_a_candidate(self):
        crash = _step_seed(version="155.0", raw_crash={"json_dump": {}})
        self.assertNotIn("this report is on", second_opinion._user_prompt(
            crash, {"node": "ab9673b0a48d"}))
        self.assertNotIn("this report is on", second_opinion._user_prompt(
            _step_seed(raw_crash={"json_dump": {}}), None))
