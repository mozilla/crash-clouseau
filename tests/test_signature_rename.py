# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A renamed signature, end to end: the seed's family facts reach the recorder, the crash brief,
the age gate, the filed bug's sentences, the volume count, the filer's venue and the spike sweep.

The fixture is bug 2073210 (2026-09-17, release, our first `actionable` filing): every sandbox
`CHECK()` failure had moved from `sandbox::InterceptionManager::PatchNtdll` -- bug 1737467, open
since 2021 -- onto `logging::(anonymous namespace)::CheckLogMessage::~CheckLogMessage` when the
Chromium sandbox update added a class Socorro's skip list did not know. :bobowen, comment 1:
"This looks like a signature change for the various CHECK messages ... We'll need to get these
signatures ignored." Everything below is what should have happened instead.

Run: DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
     uv run python -m unittest tests.test_signature_rename
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, models, report_bug, sigfamily, spike_report  # noqa: E402
from crashclouseau import spikes  # noqa: E402
from crashclouseau.agent import orchestrator as orch, spike_escalation as se, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind, Candidate, Claim, Confidence, Decision, Dossier, SearchfoxCitation, Verdict,
)
from tests.test_autofile import _PREVIEW, _Base as _AutofileBase, _bug  # noqa: E402
from tests.test_spike_escalation import _FakeEscalation, _FilerBase, _esc, _row  # noqa: E402

CHECK = "logging::(anonymous namespace)::CheckLogMessage::~CheckLogMessage"
PATCH = "sandbox::InterceptionManager::PatchNtdll"
NORETURN = "logging::CheckNoreturnError::~CheckNoreturnError"
HANDOFF_BUILD = "20260812182057"
CRASH_BUILD = "20260903215306"
CHANGE = "the new frame `{}` took over the name from `{}`".format(CHECK, PATCH)

PRED = {"signature": PATCH, "relation": "pushed-down", "status": "handoff", "before": 374,
        "after": 0, "after_dates": 164, "expected_after": 481.0, "total_channel": 5374,
        "first_build": "20260722120000", "older": True, "alignment": "build", "s_after": 5220,
        "first_seen_ever": "20250310180126", "first_seen_ever_date": "2025-03-10",
        "change": CHANGE, "changed_frames": {"added": [CHECK], "removed": [PATCH]}}
SIB = {"signature": NORETURN, "relation": "pushed-down", "status": "younger", "before": 0,
       "after": 3, "first_build": "20260824154132", "total_all_channels": 6}


def _seed(**over):
    seed = {"uuid": "u-1", "signature": CHECK, "channel": "release", "product": "Firefox",
            "buildid": CRASH_BUILD, "version": "155.0.1", "stack": "#0 f a:1", "is_offstack": False,
            "signature_first_seen_buildid": HANDOFF_BUILD,
            "signature_first_seen_channel": HANDOFF_BUILD,
            "signature_first_seen_ever": HANDOFF_BUILD,
            "signature_predecessors": [PRED], "signature_siblings": [SIB],
            "signature_family_first_seen_ever": "20250310180126",
            "signature_handoff_build": HANDOFF_BUILD, "signature_handoff_alignment": "build",
            "signature_fan_in": 1, "signature_family_lookup": "ok"}
    seed.update(over)
    return seed


FAMILY = {"predecessors": [PRED], "siblings": [SIB], "s_first_build": HANDOFF_BUILD,
          "family_first_seen_ever": "20250310180126", "alignment": "build", "fan_in": 1,
          "lookup": "ok"}


# ---------------------------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------------------------
class TestTheRecorder(unittest.TestCase):
    def test_the_family_reaches_the_corroborations_with_literal_keys(self):
        d = Dossier(crash={"uuid": "u", "signature": CHECK, "frames": []})
        orch._record_signature_age_facts(d, _seed())
        c = d.corroborations
        self.assertEqual(c["signature_family_lookup"], "ok")
        self.assertEqual(c["signature_predecessor"], PATCH)
        self.assertEqual(c["signature_predecessors"], [PATCH])
        self.assertEqual((c["signature_predecessor_before"], c["signature_predecessor_after"]),
                         (374, 0))
        self.assertEqual(c["signature_predecessor_change"], CHANGE)
        self.assertEqual(c["signature_predecessor_first_seen_ever"], "20250310180126")
        self.assertEqual(c["signature_handoff_build"], HANDOFF_BUILD)
        self.assertEqual(c["signature_handoff_alignment"], "build")
        self.assertEqual(c["signature_fan_in"], 1)
        self.assertEqual(c["signature_siblings_live"], [NORETURN])
        self.assertEqual(c["signature_family_first_seen_ever"], "20250310180126")
        # `novelty_facts` withdraws "new" for the rename, beside its two older reasons.
        self.assertIn("predecessor_handoff", c["signature_novelty_unreliable"].split(","))
        # ...and the persisted facts rebuild the family the filer searches under.
        fam = sigfamily.family_from_corroborations(c)
        self.assertEqual(sigfamily.spellings(fam), [PATCH, NORETURN])
        self.assertEqual(fam["s_first_build"], HANDOFF_BUILD)

    def test_a_failed_or_empty_lookup_records_only_its_status(self):
        d = Dossier(crash={"uuid": "u", "signature": CHECK, "frames": []})
        orch._record_signature_age_facts(d, _seed(signature_predecessors=[], signature_siblings=[],
                                                  signature_family_lookup="failed",
                                                  signature_handoff_build=None,
                                                  signature_family_first_seen_ever=None))
        c = d.corroborations
        self.assertEqual(c["signature_family_lookup"], "failed")
        for key in ("signature_predecessor", "signature_predecessors", "signature_siblings_live",
                    "signature_handoff_build", "signature_family_first_seen_ever"):
            self.assertNotIn(key, c)
        self.assertNotIn("predecessor_handoff", c.get("signature_novelty_unreliable", ""))
        # A seed from before the instrument (no lookup key at all) writes nothing about it.
        d2 = Dossier(crash={"uuid": "u", "signature": CHECK, "frames": []})
        seed = _seed()
        for key in list(seed):
            if key.startswith("signature_") and key not in ("signature_first_seen_buildid",
                                                            "signature_first_seen_ever"):
                del seed[key]
        orch._record_signature_age_facts(d2, seed)
        self.assertNotIn("signature_family_lookup", d2.corroborations)
        self.assertIsNone(sigfamily.family_from_corroborations(d2.corroborations))


# ---------------------------------------------------------------------------------------------
# The crash brief
# ---------------------------------------------------------------------------------------------
class TestTheBrief(unittest.TestCase):
    def test_the_rename_block_replaces_the_age_block(self):
        text = "\n".join(triage._signature_age_lines(_seed()))
        self.assertIn("SIGNATURE RENAME: this NAME is new", text)
        self.assertIn("in build 20260812182057 (2026-08-12)", text)
        self.assertIn("it is `{}` under a new name".format(PATCH), text)
        self.assertIn("had 374 reports on the builds of the 28 days before that build and 0 on"
                      " builds from it on, against 5220 under this name", text)
        self.assertIn(CHANGE, text)
        self.assertIn("`{}` was first seen anywhere in build 20250310180126 (2025-03-10)".format(PATCH), text)
        self.assertIn("before the build that produced this crash", text)
        self.assertIn("BUILD-aligned", text)
        self.assertIn("is the RENAMER, not the regressor", text)
        self.assertIn("name it as the EXPOSER", text)
        self.assertNotIn("SIGNATURE AGE:", text)
        self.assertNotIn("genuinely trustworthy", text)

    def test_a_date_aligned_rename_forbids_a_regressor_for_the_appearance(self):
        text = "\n".join(triage._signature_age_lines(_seed(signature_handoff_alignment="date")))
        self.assertIn("DATE-aligned", text)
        self.assertIn("No changeset can be the cause of that", text)
        self.assertIn("do not name a regressor for the appearance of this name", text)
        self.assertNotIn("RENAMER", text)

    def test_a_catch_all_says_so_and_names_the_frame(self):
        other = dict(PRED, signature="sandbox::TargetServicesBase::LowerToken", before=90)
        text = "\n".join(triage._signature_age_lines(
            _seed(signature_predecessors=[PRED, other], signature_fan_in=2)))
        self.assertIn("absorbed 2 previously DISTINCT signatures", text)
        self.assertIn("CATCH-ALL minted by a generic frame (`{}`)".format(CHECK), text)
        self.assertIn("Socorro skip-list entry", text)

    def test_without_a_predecessor_the_brief_is_what_it_was(self):
        text = "\n".join(triage._signature_age_lines(
            _seed(signature_predecessors=[], signature_handoff_build=None)))
        self.assertNotIn("SIGNATURE RENAME", text)
        self.assertIn("SIGNATURE AGE:", text)

    def test_the_blind_second_opinion_sees_the_same_block(self):
        text = "\n".join(triage._crash_facts(_seed(raw_crash={"json_dump": {}}, frames=[])))
        self.assertIn("SIGNATURE RENAME", text)


# ---------------------------------------------------------------------------------------------
# The age gate's clock
# ---------------------------------------------------------------------------------------------
_SF = SearchfoxCitation(permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central")


def _lead(decision=Decision.lead, confidence=Confidence.probable):
    claims = {}
    if decision == Decision.actionable:
        claims = {"mechanism": Claim(statement="CHECK fails when the hook is missing", citations=[_SF]),
                  "consistency": Claim(statement="every report is a sandbox CHECK", citations=[_SF])}
    return Dossier(candidate=Candidate(node="abc123def456", bug=42),
                   verdict=Verdict(decision=decision, confidence=confidence,
                                   needinfo_draft="could you take a look?", **claims))


_AGE_CFG = {"enabled": True, "min_age_days": 7, "other_channel_floor": 20}


class TestTheGateClock(unittest.TestCase):
    def _gate(self, dossier, seed):
        # Off nightly the gate resolves the candidate's LANDING date over hg; the seeded date
        # stands in for it here.
        with mock.patch.object(orch.config, "get_agent_signature_age", return_value=_AGE_CFG), \
                mock.patch("crashclouseau.sigage.pushdate_for_node", return_value=None):
            orch._apply_signature_age_gate(dossier, seed)

    def test_a_handoff_predecessor_moves_the_clock_back(self):
        # The renamer landed two days BEFORE this name was first seen -- healthy on the name's
        # clock -- and 19 days after the crash's, on the predecessor's first build.
        landed = datetime(2026, 8, 10, tzinfo=timezone.utc)
        d = _lead()
        self._gate(d, _seed(candidate_pushdates={"abc123def456": landed}))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        c = d.corroborations
        self.assertTrue(c["stale_signature"])
        self.assertEqual(c["stale_signature_family_clock"], PATCH)
        self.assertEqual(c["signature_first_seen_buildid"], "20260722120000")
        self.assertAlmostEqual(c["candidate_landed_after_first_seen_days"], 18.5, delta=0.6)
        self.assertTrue(c["stale_signature_clamped"])

    def test_a_coexisting_sibling_never_moves_it(self):
        landed = datetime(2026, 8, 10, tzinfo=timezone.utc)
        d = _lead()
        older = dict(SIB, status="older", first_build="20260101000000", before=40, after=40)
        self._gate(d, _seed(signature_predecessors=[], signature_siblings=[older],
                            signature_handoff_build=None,
                            candidate_pushdates={"abc123def456": landed}))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertNotIn("stale_signature", d.corroborations)

    def test_an_actionable_verdict_on_a_renamed_crash_is_pre_existing(self):
        landed = datetime(2026, 8, 10, tzinfo=timezone.utc)
        d = _lead(Decision.actionable, Confidence.high)
        self._gate(d, _seed(candidate_pushdates={"abc123def456": landed}))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pre_existing)
        self.assertIn("build 20260722120000", d.verdict.abstain_reason)


# ---------------------------------------------------------------------------------------------
# The filed bug's sentences and its volume
# ---------------------------------------------------------------------------------------------
def _corroborations(**over):
    d = Dossier(crash={"uuid": "u", "signature": CHECK, "frames": []})
    orch._record_signature_age_facts(d, _seed(**over))
    return d.corroborations


class TestTheBugText(unittest.TestCase):
    def test_the_age_note_leads_with_the_old_name(self):
        note = report_bug.build_signature_age_note(_corroborations(), CRASH_BUILD)
        self.assertTrue(note.startswith("This signature is a new NAME for an older crash: since "
                                        "build 20260812182057 (2026-08-12) the crash previously "
                                        "reported as `{}` reports under it".format(PATCH)))
        self.assertIn(CHANGE, note)
        self.assertIn("`{}` had 374 reports on the builds of the 28 days before and 0 since".format(PATCH), note)
        self.assertIn("first recorded in build 20250310180126 (2025-03-10), 542 days before the "
                      "build above", note)
        self.assertIn("kept reporting on older builds afterwards", note)
        self.assertNotIn("This signature is new", note)

    def test_the_actionable_onset_line(self):
        note = report_bug.build_signature_since_note(_corroborations(), CRASH_BUILD)
        self.assertIn("This crash has been reported since build 20250310180126 (2025-03-10), "
                      "under the signature `{}` until build 20260812182057 (2026-08-12) and "
                      "under the signature above since".format(PATCH), note)

    def test_the_date_aligned_and_catch_all_sentences(self):
        other = dict(PRED, signature="sandbox::TargetServicesBase::LowerToken")
        note = report_bug.build_signature_age_note(_corroborations(
            signature_handoff_alignment="date", signature_predecessors=[PRED, other],
            signature_fan_in=2), CRASH_BUILD)
        self.assertIn("absorbed 2 previously distinct signatures "
                      "(`sandbox::TargetServicesBase::LowerToken`)", note)
        self.assertIn("Socorro skip-list entry", note)
        self.assertIn("stopped on every live build at once on 2026-08-12", note)
        self.assertNotIn("kept reporting", note)

    def test_the_timing_note_names_the_old_name_when_the_clock_was_its(self):
        note = report_bug.build_stale_signature_note({
            "stale_signature": True, "candidate_landed_after_first_seen_days": 18.5,
            "signature_first_seen_buildid": "20260722120000",
            "stale_signature_family_clock": PATCH})
        self.assertIn("already being reported under its earlier name `{}` in build "
                      "20260722120000, 18 days before".format(PATCH), note)
        plain = report_bug.build_stale_signature_note({
            "stale_signature": True, "candidate_landed_after_first_seen_days": 18.5,
            "signature_first_seen_buildid": "20260722120000"})
        self.assertNotIn("earlier name", plain)

    def test_without_a_predecessor_the_notes_are_unchanged(self):
        c = _corroborations(signature_predecessors=[], signature_handoff_build=None)
        self.assertNotIn("new NAME", report_bug.build_signature_age_note(c, CRASH_BUILD))
        self.assertNotIn("under the signature", report_bug.build_signature_since_note(c, CRASH_BUILD))


class _FakeStatsSearch:
    """`socorro.SuperSearch` for `fetch_signature_stats`: the build aggregation, then the
    sibling facet."""
    calls = []

    def __init__(self, params, handler, handlerdata):
        _FakeStatsSearch.calls.append(params)
        if params.get("_facets") == "signature":
            handler({"facets": {"signature": [{"term": PATCH, "count": 9},
                                              {"term": "Other::sig", "count": 4}]}}, handlerdata)
        else:
            handler({"facets": {"build_id": [
                {"term": int(CRASH_BUILD), "count": 21,
                 "facets": {"install_time": [{"term": "t{}".format(i), "count": 1}
                                             for i in range(7)],
                            "cardinality_install_time": {"value": 7}}}]}}, handlerdata)

    def wait(self):
        return None


class TestTheVolume(unittest.TestCase):
    INFO = {"signature": CHECK, "buildid": CRASH_BUILD, "product": "Firefox",
            "channel": "release", "version": "155.0.1"}

    def setUp(self):
        report_bug._STATS_CACHE.clear()
        _FakeStatsSearch.calls = []
        self.addCleanup(report_bug._STATS_CACHE.clear)

    def test_the_siblings_are_a_second_sentence_and_the_first_is_untouched(self):
        with mock.patch.object(report_bug.socorro, "SuperSearch", _FakeStatsSearch):
            first, stats = report_bug.fetch_signature_stats("u-vol", self.INFO, siblings=[PATCH, CHECK])
        self.assertEqual((first, stats["count"], stats["installs"]), (True, 21, 7))
        self.assertEqual(stats["siblings"], {PATCH: 9})
        # The sibling query asks for the siblings only, on the same builds, channel and product.
        sib_params = _FakeStatsSearch.calls[1]
        self.assertEqual(sib_params["signature"], ["=" + PATCH])
        self.assertEqual((sib_params["build_id"], sib_params["release_channel"]),
                         (">=" + CRASH_BUILD, "release"))
        sentence = report_bug.build_stats_sentence(first, stats, self.INFO)
        self.assertTrue(sentence.startswith(
            "There are 21 crashes (from 7 installations) in 155.0.1 with buildid {}.".format(CRASH_BUILD)))
        self.assertIn("9 more reports in the same period sit under sibling spelling of this "
                      "signature: `{}` (9).".format(PATCH), sentence)

    def test_no_siblings_means_the_old_sentence_and_one_request(self):
        with mock.patch.object(report_bug.socorro, "SuperSearch", _FakeStatsSearch):
            first, stats = report_bug.fetch_signature_stats("u-vol2", self.INFO)
        self.assertEqual(len(_FakeStatsSearch.calls), 1)
        self.assertNotIn("siblings", stats)
        self.assertEqual(report_bug.build_stats_sentence(first, stats, self.INFO),
                         "There are 21 crashes (from 7 installations) in 155.0.1 with buildid {}.".format(CRASH_BUILD))


# ---------------------------------------------------------------------------------------------
# The filer
# ---------------------------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TestTheVenueSearch(unittest.TestCase):
    OLD_BUG = {"id": 1737467, "summary": "Crash in [@ sandbox::InterceptionManager::PatchNtdll]",
               "creation_time": "2021-10-25T09:00:00Z", "product": "Core", "keywords": [],
               "cf_crash_signature": "[@ logging::LogMessage::~LogMessage]\n[@ {}]".format(PATCH),
               "regressed_by": []}
    NEW_BUG = {"id": 2073210, "summary": "Crash in [@ {}]".format(CHECK),
               "creation_time": "2026-09-17T17:45:01Z", "product": "Core", "keywords": [],
               "cf_crash_signature": "[@ {}]".format(CHECK), "regressed_by": []}
    OTHER = {"id": 5, "summary": "Crash in [@ Unrelated::Thing]", "creation_time": "2026-09-01T00:00:00Z",
             "product": "Core", "keywords": [], "cf_crash_signature": "[@ Unrelated::Thing]",
             "regressed_by": []}

    def test_the_old_name_is_asked_for_and_its_bug_is_dated_by_the_handoff(self):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen.update(params or {})
            return _Resp({"bugs": [self.OLD_BUG, self.NEW_BUG, self.OTHER]})

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=fake_get), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature",
                                  return_value=[]) as dups:
            rows = bugzilla_apply._open_bugs_for_signature(CHECK, family=FAMILY)
        values = {seen[k] for k in seen if k.startswith("v")}
        self.assertEqual(values, {CHECK, "[@ " + CHECK, PATCH, "[@ " + PATCH,
                                  NORETURN, "[@ " + NORETURN})
        self.assertEqual([r["id"] for r in rows], [1737467, 2073210])
        old, new = rows
        self.assertEqual((old["via_signature"], old["via_relation"]), (PATCH, "handoff"))
        self.assertEqual(old["venue_since"], "2026-08-12T18:20:57+00:00")
        self.assertNotIn("via_signature", new)
        self.assertNotIn("venue_since", new)
        # The dup-following asks under every name too.
        self.assertIn(PATCH, dups.call_args.kwargs["spellings"])

    def test_a_sibling_venue_keeps_its_own_clock(self):
        fam = {"predecessors": [], "siblings": [SIB], "s_first_build": HANDOFF_BUILD}
        sib_bug = dict(self.OLD_BUG, id=77, cf_crash_signature="[@ {}]".format(NORETURN))
        with mock.patch.object(bugzilla_apply.net, "get", return_value=_Resp({"bugs": [sib_bug]})), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature", return_value=[]):
            rows = bugzilla_apply._open_bugs_for_signature(CHECK, family=fam)
        self.assertEqual((rows[0]["via_signature"], rows[0]["via_relation"]), (NORETURN, "sibling"))
        self.assertNotIn("venue_since", rows[0])

    def test_no_family_is_the_old_query(self):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen.update(params or {})
            return _Resp({"bugs": []})

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=fake_get), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature", return_value=[]):
            bugzilla_apply._open_bugs_for_signature(CHECK)
        self.assertEqual({k for k in seen if k.startswith("v")}, {"v1", "v2"})

    def test_the_spelling_map_excludes_the_signatures_own_and_is_bounded(self):
        m = bugzilla_apply._family_spelling_map(CHECK, FAMILY)
        self.assertEqual(set(m), {PATCH, NORETURN})
        self.assertEqual(m[PATCH]["relation"], "handoff")
        self.assertEqual(m[NORETURN], {"via": NORETURN, "relation": "sibling", "since": None})
        many = {"predecessors": [dict(PRED, signature="P{}::f".format(i)) for i in range(20)],
                "s_first_build": HANDOFF_BUILD}
        self.assertEqual(len(bugzilla_apply._family_spelling_map(CHECK, many)),
                         bugzilla_apply._MAX_FAMILY_SPELLINGS)
        self.assertEqual(bugzilla_apply._family_spelling_map(CHECK, None), {})
        self.assertEqual(bugzilla_apply._family_spellings(CHECK, FAMILY), [CHECK, PATCH, NORETURN])

    def test_unsymbolicated_covers_module_only_names(self):
        for sig in ("libxul.so (deleted) | libxul.so (deleted) | libnspr4.so (deleted)",
                    "xul.dll | _PR_MD_UNLOCK | PR_Unlock | xul.dll", "libvulkan_radeon.so",
                    "@0xe2ba40f948"):
            self.assertTrue(bugzilla_apply._is_unsymbolicated(sig), sig)
        for sig in ("OOM | unknown | memcpy_repmovs_Intel | RTCEncodedFrameBase", CHECK,
                    "mozilla::detail::MutexImpl::mutexLock"):
            self.assertFalse(bugzilla_apply._is_unsymbolicated(sig), sig)


class TestAttachSignature(unittest.TestCase):
    def test_append_never_rewrite(self):
        puts = []
        with mock.patch.object(bugzilla_apply, "_signature_field",
                               return_value="[@ logging::LogMessage::~LogMessage]\n[@ {}]".format(PATCH)), \
                mock.patch.object(bugzilla_apply, "_put_bug",
                                  side_effect=lambda b, c, t: puts.append((b, c)) or b):
            self.assertEqual(bugzilla_apply._attach_signature(1737467, CHECK, "tok"), "attached")
        self.assertEqual(puts, [(1737467, {"cf_crash_signature":
                                           "[@ logging::LogMessage::~LogMessage]\n[@ {}]\n[@ {}]".format(PATCH, CHECK)})])

    def test_already_there_in_any_spelling_and_failures(self):
        with mock.patch.object(bugzilla_apply, "_signature_field",
                               return_value="[@ {} ]".format(CHECK)), \
                mock.patch.object(bugzilla_apply, "_put_bug") as put:
            self.assertEqual(bugzilla_apply._attach_signature(1737467, CHECK, "tok"), "already")
        put.assert_not_called()
        with mock.patch.object(bugzilla_apply, "_signature_field", return_value=None):
            self.assertEqual(bugzilla_apply._attach_signature(1737467, CHECK, "tok"), "failed")
        with mock.patch.object(bugzilla_apply, "_signature_field", return_value=""), \
                mock.patch.object(bugzilla_apply, "_put_bug", side_effect=RuntimeError("503")):
            self.assertEqual(bugzilla_apply._attach_signature(1737467, CHECK, "tok"), "failed")
        self.assertEqual(bugzilla_apply._attach_signature(None, CHECK, "tok"), "failed")


_VIA_ROW = dict(_bug(1737467, created="2021-10-25T09:00:00Z"), via_signature=PATCH,
                via_relation="handoff", venue_since="2026-08-12T18:20:57+00:00")
_INFO_CHECK = {"uuid": "u-1", "signature": CHECK, "channel": "release", "product": "Firefox",
               "buildid": CRASH_BUILD, "version": "155.0.1"}


def _dossier():
    d = Dossier(crash={"uuid": "u-1", "signature": CHECK, "frames": []},
                candidate=Candidate(node="n", bug=42))
    orch._record_signature_age_facts(d, _seed())
    return {"candidate": {"node": "n", "bug": 42}, "corroborations": d.corroborations,
            "crash": _seed()}


class TestTheFilerOnARenamedCrash(_AutofileBase):
    def setUp(self):
        super().setUp()
        self.attached = []
        for p in (mock.patch.object(bugzilla_apply, "_signature_field",
                                    return_value="[@ {}]".format(PATCH)),
                  mock.patch.object(bugzilla_apply.config, "autofile_channel_declared",
                                    return_value=True),
                  mock.patch.object(bugzilla_apply.config, "autofile_product_declared",
                                    return_value=True),
                  mock.patch.object(bugzilla_apply.config, "autofile_product_held",
                                    return_value=False),
                  mock.patch.object(bugzilla_apply.models.Dossier, "run_options",
                                    return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def _file(self, mode="comment", verdict="lead", confidence=70):
        cfg = {"enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit", "actionable"],
               "needinfo": True, "daily_cap": None, "comment_on_existing": mode,
               "comment_max_bug_age_days": 30}
        bugzilla_apply.config.get_agent_autofile.return_value = cfg
        return bugzilla_apply.autofile_bug("u-1", _INFO_CHECK, {}, _dossier(), verdict, confidence)

    def test_the_old_names_bug_is_the_venue_and_gets_the_new_name(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_VIA_ROW]
        bugzilla_apply._candidate_landed.return_value = datetime(2026, 8, 10, tzinfo=timezone.utc)
        res = self._file()
        self.assertEqual((res["filed"], res["bug"], res["mode"]), (True, 1737467, "comment_on_existing"))
        self.assertEqual((res["venue_via_signature"], res["venue_via_relation"]), (PATCH, "handoff"))
        self.assertEqual(res["signature_attached"], "attached")
        # The family reached the venue search...
        self.assertEqual(bugzilla_apply._open_bugs_for_signature.call_args.kwargs["family"]["s_first_build"],
                         HANDOFF_BUILD)
        # ...the comment says why it is here...
        bug, text = self.comments[0]
        self.assertEqual(bug, 1737467)
        self.assertTrue(text.startswith(_PREVIEW["comment"]))
        self.assertIn("_Posted here because this crash reports under the signature `{}` since "
                      "build 20260812182057 (2026-08-12): {}. This bug carried the earlier name "
                      "`{}`; `[@ {}]` has been added to its crash signatures._".format(
                          CHECK, CHANGE, PATCH, CHECK), text)
        # ...and the name was appended, in its own PUT, after the old entries.
        attach = [c for b, c in self.puts if "cf_crash_signature" in c]
        self.assertEqual(attach, [{"cf_crash_signature": "[@ {}]\n[@ {}]".format(PATCH, CHECK)}])
        self.assertEqual(self.created, [])

    def test_a_skip_channel_declines_and_names_the_old_name(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_VIA_ROW]
        res = self._file(mode="skip")
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"],
                         "open bug 1737467 (on this crash's earlier name `{}`) exists".format(PATCH))
        self.assertEqual(res["venue_via_signature"], PATCH)
        self.assertEqual((self.comments, self.puts, self.created), ([], [], []))

    def test_an_actionable_crash_is_not_filed_past_the_old_names_bug(self):
        # 2073210 itself: `actionable`, release, and bug 1737467 open on `PatchNtdll`.
        bugzilla_apply._open_bugs_for_signature.return_value = [_VIA_ROW]
        with mock.patch.object(report_bug, "fetch_signature_stats", return_value=(True, {"count": 5220, "installs": 800})):
            res = self._file(mode="skip", verdict="actionable", confidence=70)
        self.assertFalse(res["filed"])
        self.assertIn("open bug 1737467 (on this crash's earlier name `{}`) exists; an actionable "
                      "crash is filed only where no bug is".format(PATCH), res["skipped"])
        self.assertEqual(self.created, [])

    def test_a_venue_through_the_signature_itself_is_untouched(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(2073210)]
        res = self._file()
        self.assertEqual((res["filed"], res["bug"]), (True, 2073210))
        self.assertNotIn("venue_via_signature", res)
        self.assertNotIn("Posted here because", self.comments[0][1])
        self.assertEqual([c for b, c in self.puts if "cf_crash_signature" in c], [])

    def test_the_fixed_and_train_lookups_are_asked_under_every_name(self):
        bugzilla_apply._open_bugs_for_signature.return_value = []
        self._file(mode="skip")
        self.assertEqual(bugzilla_apply._fixed_after_build_bug.call_args.kwargs["spellings"],
                         [CHECK, PATCH, NORETURN])
        self.assertEqual(bugzilla_apply._known_on_train_bug.call_args.kwargs["spellings"],
                         [CHECK, PATCH, NORETURN])

    def test_our_own_bug_under_the_other_spelling_stops_a_skip_channel(self):
        # 2072875: the Linux spelling of the Windows name we had filed (restricted) two days
        # earlier. Same crash, our bug, different string.
        bugzilla_apply.models.Dossier.already_filed_for_signature.side_effect = (
            lambda sig, **kw: {"uuid": "u-0", "bug": 2072488} if sig == PATCH else None)
        res = self._file(mode="skip")
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "already filed bug 2072488 for this crash under its other "
                                         "name `{}` on release".format(PATCH))


# ---------------------------------------------------------------------------------------------
# The spike path
# ---------------------------------------------------------------------------------------------
class TestTheSweepDeclinesARebucketing(unittest.TestCase):
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
            mock.patch.object(spikes, "build_history", return_value=[]),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[]),
            mock.patch.object(se, "resolve_venue_below_public", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _sweep(self, rows, room=2):
        with mock.patch.object(models.Selection, "escalation_candidates", return_value=rows):
            return se._sweep_channel("Firefox", "nightly", self.cfg, room)

    def test_the_handoff_is_recorded_done_and_nothing_is_spent(self):
        handoff = dict(PRED, s_first_build="20260903093145")
        with mock.patch.object(se, "_handoff_for_spike", return_value=handoff) as hf:
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 0)
        hf.assert_called_once_with("mozilla::Foo::Bar", "u-1", "Firefox", "nightly", "20260903093145")
        self.assertEqual(self.enqueued, [])
        row = self.created[0]
        self.assertEqual((row.status, row.uuid, row.payload["predecessor"]), ("done", "u-1", PATCH))
        self.assertEqual(row.payload["skipped"],
                         "re-bucketing from `{}`: that signature had 374 reports on the builds of the "
                         "28 days before build 20260903093145 and 0 on builds from it on ({}) -- the "
                         "spike is an old crash under a new name, not a new crash".format(PATCH, CHANGE))
        self.assertEqual((row.payload["predecessor_before"], row.payload["predecessor_after"]), (374, 0))

    def test_no_handoff_escalates_as_before(self):
        with mock.patch.object(se, "_handoff_for_spike", return_value=None):
            self.assertEqual(self._sweep([_row(32, [1, 0, 2], 21)]), 1)
        self.assertEqual(self.enqueued, [1])

    def test_the_lookup_reads_the_representative_reports_proto(self):
        with mock.patch("crashclouseau.inspector.get_crash_data",
                        return_value={"proto_signature": "A | B"}), \
                mock.patch.object(sigfamily, "handoff_for_spike", return_value=PRED) as hf:
            self.assertEqual(se._handoff_for_spike("S", "u-1", "Firefox", "nightly", "b1"), PRED)
        hf.assert_called_once_with("S", "A | B", "Firefox", "nightly", "b1")
        # A processed crash Socorro will not give us is not a reason to skip the lookup.
        with mock.patch("crashclouseau.inspector.get_crash_data", side_effect=RuntimeError("503")), \
                mock.patch.object(sigfamily, "handoff_for_spike", return_value=None) as hf:
            self.assertIsNone(se._handoff_for_spike("S", "u-1", "Firefox", "nightly", "b1"))
        hf.assert_called_once_with("S", None, "Firefox", "nightly", "b1")
        with mock.patch.object(config, "get_agent_signature_family",
                               return_value={"enabled": False, "days": 182, "max_candidates": 8}), \
                mock.patch("crashclouseau.inspector.get_crash_data") as fetch:
            self.assertIsNone(se._handoff_for_spike("S", "u-1", "Firefox", "nightly", "b1"))
        fetch.assert_not_called()


class TestTheSpikeReport(unittest.TestCase):
    BRIEF = {"signature": CHECK, "channel": "release", "product": "Firefox", "version": "155.0.1",
             "buildid": HANDOFF_BUILD, "build_day": "2026-08-12", "uuid": "u-1",
             "first_seen_ever": HANDOFF_BUILD, "first_seen_channel": HANDOFF_BUILD,
             "spike": {"kind": "build_day", "count": 50, "installs": 50, "baseline": [0, 0, 0]}}

    def test_a_renamed_appearance_is_not_new(self):
        self.assertTrue(spike_report.is_new_signature(self.BRIEF))
        self.assertFalse(spike_report.is_new_signature(dict(self.BRIEF, signature_predecessors=[PRED])))
        for status in ("older", "coexisting", "undecided"):
            self.assertFalse(spike_report.is_new_signature(
                dict(self.BRIEF, signature_siblings=[dict(SIB, status=status)])), status)
        self.assertTrue(spike_report.is_new_signature(
            dict(self.BRIEF, signature_siblings=[SIB])), "a younger spelling says nothing")
        p = spike_report.build_spike_preview(dict(self.BRIEF, signature_predecessors=[PRED],
                                                  signature_family=FAMILY), None,
                                             product="Core", component="Security: Process Sandboxing")
        self.assertEqual(p["title"], "Crash in [@ {}]".format(CHECK))

    def test_the_age_sentence_names_the_old_name(self):
        text = spike_report.signature_age_sentence(dict(self.BRIEF, signature_predecessors=[PRED],
                                                        signature_family=FAMILY))
        self.assertEqual(text, "This signature is a new name for an older crash: until build "
                               "20260812182057 (2026-08-12) it reported as `{}` (374 reports on the "
                               "builds of the 28 days before, 0 since; {}); `{}` was first recorded "
                               "in build 20250310180126 (2025-03-10).".format(PATCH, CHANGE, PATCH))
        dated = spike_report.signature_age_sentence(dict(
            self.BRIEF, signature_predecessors=[PRED], signature_family=dict(FAMILY, alignment="date")))
        self.assertIn("stopped on every live build at once", dated)
        self.assertIn("is new on release", spike_report.signature_age_sentence(self.BRIEF))

    def test_the_persisted_brief_carries_the_names(self):
        pub = se._public_brief(dict(self.BRIEF, signature_predecessors=[PRED], signature_siblings=[SIB]))
        self.assertEqual((pub["signature_predecessors"], pub["signature_siblings"], pub["new_signature"]),
                         ([PATCH], [NORETURN], False))


class TestTheSpikeFilerOnARenamedCrash(_FilerBase):
    def test_the_old_names_bug_gets_the_comment_and_the_new_name(self):
        brief = dict(self.brief, signature_family=FAMILY, signature_predecessors=[PRED])
        puts = []
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                dict(_VIA_ROW, id=1737467)]), \
                mock.patch.object(bugzilla_apply, "_signature_field",
                                  return_value="[@ {}]".format(PATCH)), \
                mock.patch.object(bugzilla_apply, "_put_bug",
                                  side_effect=lambda b, c, t: puts.append((b, c)) or b):
            res = se.file_spike_bug(_esc(), brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["bug"], res["venue_kind"]), ("spike_comment", 1737467, "open"))
        self.assertEqual((res["venue_via_signature"], res["signature_attached"]), (PATCH, "attached"))
        self.assertIn("_Posted here because this crash reports under the signature "
                      "`mozilla::Foo::Bar` since build 20260812182057", self.comments[0][1])
        self.assertEqual(puts, [(1737467, {"cf_crash_signature": "[@ {}]\n[@ mozilla::Foo::Bar]".format(PATCH)})])
        self.assertEqual(self.created, [])

    def test_a_skip_channel_names_the_old_name_in_the_decline(self):
        brief = dict(self.brief, channel="beta", signature_family=FAMILY)
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[_VIA_ROW]), \
                mock.patch.object(config, "get_agent_spike_escalation",
                                  return_value=dict(config.get_agent_spike_escalation(),
                                                    comment_on_existing="skip")):
            res = se.file_spike_bug(_esc(channel="beta"), brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "open bug 1737467 (on this crash's earlier name `{}`) exists".format(PATCH))
        self.assertEqual(res["venue_via_signature"], PATCH)
        self.assertEqual(self.comments, [])


if __name__ == "__main__":
    unittest.main()
