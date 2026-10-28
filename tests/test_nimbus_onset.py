# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Nimbus recipe matching, bug notes and owner routing."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import nimbus, report_bug  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import Dossier  # noqa: E402
from tests import test_actionable_verdict as tav  # noqa: E402

SIG = "autofill::db::addresses::update_internal_address"
NODE = "d707487029a4"
FIRST = datetime(2026, 10, 27, 17, 42, 40, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 27, 18, 3, tzinfo=timezone.utc)

# Experimenter v6, trimmed to the fields `compact` reads.
ROLLOUT = {
    "slug": "addresses-storage-migration-nightly-rollout", "isRollout": True, "channel": "",
    "featureIds": ["autofill-addresses-rust-storage"],
    "publishedDate": "2026-10-27T17:16:04.802693Z",
    "targeting": "(browserSettings.update.channel in [\"nightly\"]) && ((experiment.slug in "
                 "activeRollouts) || ((version|versionCompare('160.!') >= 0)))",
    "bucketConfig": {"count": 10000, "total": 10000},
    "branches": [{"slug": "control", "features": [
        {"featureId": "autofill-addresses-rust-storage", "value": {"enabled": True}}]}],
}
UNRELATED = {
    "slug": "relevant-sponsored-stories-on-nightly", "isRollout": False, "channel": "",
    "featureIds": ["newtabSponsoredStories"], "publishedDate": "2026-10-27T16:47:56Z",
    "targeting": "(browserSettings.update.channel in [\"nightly\"]) && "
                 "(version|versionCompare('160.!') >= 0)",
    "bucketConfig": {"count": 5000, "total": 10000},
    "branches": [{"slug": "treatment", "features": [
        {"featureId": "newtabSponsoredStories", "value": {"show": True}}]}],
}
LAUNCH_AT_LOGIN = (
    "(browserSettings.update.channel in [\"release\"]) && ((experiment.slug in activeRollouts) "
    "|| ((os.isWindows && os.windowsVersion >= 10 && launchOnLoginAllowedByPolicy && (!isMSIX "
    "|| os.windowsBuildNumber >= 22000 || version|versionCompare('157.!') >= 0) && "
    "isNonStubFirstRun && (currentDate|date - profileAgeCreated|date) / 3600000 < 1) && "
    "(version|versionCompare('156.!') >= 0) && (('launch-at-login-infobar-new-user-experiment' "
    "in enrollments) == false) && (((enrollmentsMap['long-term-holdback-2026-growth-desktop'] "
    "== 'delivery')))))")
RELEASE_ONLY = dict(UNRELATED, slug="release-thing",
                    targeting="(browserSettings.update.channel == \"release\")")
AFTER_ONSET = dict(ROLLOUT, slug="later", publishedDate="2026-10-27T17:50:00Z")
TOO_OLD = dict(ROLLOUT, slug="too-old", publishedDate="2026-10-19T17:00:00Z")

MANIFEST = """\
# comment
autofill-addresses-rust-storage:
  description: Application Services (Rust) storage backend for saved addresses
  owner: dlee@mozilla.com
  hasExposure: false
  variables:
    enabled:
      description: >-
        If true, saved addresses are migrated.
      type: boolean
      setPref:
        branch: user
        pref: extensions.formautofill.addresses.storage.rust.enabled
newtabSponsoredStories:
  owner: someone@mozilla.com
  variables:
    show:
      type: boolean
      fallbackPref: browser.newtabpage.activity-stream.showSponsored
"""

# A multi-variable feature; the recipe below configures only adaptive autofill.
URLBAR_MANIFEST = """\
urlbar:
  description: The urlbar
  owner: someone@mozilla.com, other@mozilla.com
  variables:
    actionsOnboardingTimesToShow:
      type: int
      fallbackPref: browser.urlbar.quickactions.timesToShowOnboardingLabel
      description: >-
        The number of times a user should be shown the actions onboarding label.
    autoFillAdaptiveHistoryEnabled:
      type: boolean
      setPref:
        pref: browser.urlbar.autoFill.adaptiveHistory.enabled
        branch: user
    dualPrompt.onLocationChange:
      type: boolean
      fallbackPref: browser.mailto.dualPrompt.onLocationChange
    noPref:
      type: string
  schema:
    uri: resource://x
    path: y
newtabPromoCard:
  owner: a@mozilla.com
  variables:
      enabled:
        type: boolean
        setPref:
          branch: user
          pref: browser.newtabpage.promoCard.enabled
compressionDictionaries:
  variables:
    enableDictionaries:
      type: boolean
      setPref:
       branch: default
       pref: network.http.dictionaries.enable
"""
ADAPTIVE_AUTOFILL = {
    "slug": "adaptive-autofill-revamp-v2", "isRollout": False, "channel": "",
    "featureIds": ["urlbar"], "publishedDate": "2026-10-27T10:00:00Z",
    "targeting": "(browserSettings.update.channel in [\"nightly\"])",
    "bucketConfig": {"count": 10000, "total": 10000},
    "branches": [
        {"slug": "has-adaptive-autofill", "features": [
            {"featureId": "urlbar", "value": {"autoFillAdaptiveHistoryEnabled": True}}]},
        {"slug": "no-adaptive-autofill", "features": [
            {"featureId": "urlbar", "value": {"autoFillAdaptiveHistoryEnabled": False}}]}],
}

# Autofill stack with panic machinery and frames without source revisions.
FRAMES = [
    {"filename": "mozglue/static/rust/wrappers.cpp", "function": "RustMozCrash", "node": NODE},
    {"filename": "mozglue/static/rust/lib.rs", "function": "mozglue_static::panic_hook",
     "node": NODE},
    {"filename": "library/std/src/panicking.rs", "function": "std::panicking::panic_with_hook",
     "node": None},
    {"filename": "library/core/src/panicking.rs",
     "function": "core::panicking::assert_failed<usize,usize>", "node": None},
    {"filename": "third_party/application-services/components/autofill/src/db/addresses.rs",
     "function": SIG, "node": NODE},
    {"filename": "third_party/application-services/components/autofill/src/sync/address/"
                 "incoming.rs",
     "function": "autofill::sync::address::incoming::impl$0::update_local_record", "node": NODE},
    {"filename": "third_party/application-services/components/sync15/src/engine/"
                 "bridged_engine.rs",
     "function": "sync15::engine::bridged_engine::BridgedEngineWrapper::apply", "node": NODE},
    {"filename": "xpcom/threads/nsThreadPool.cpp", "function": "nsThreadPool::Run",
     "node": NODE},
]


def _facts(recipes, first=FIRST, manifest=MANIFEST, channel="nightly", version="161.0a1",
           now=NOW, first_exc=None, first_ever=None):
    compacted = [nimbus.compact(r) for r in recipes] if recipes is not None else None
    first_report = mock.Mock(side_effect=first_exc) if first_exc else mock.Mock(return_value=first)
    with mock.patch.object(nimbus, "first_report", first_report), \
            mock.patch.object(nimbus, "live_recipes", return_value=compacted), \
            mock.patch.object(nimbus, "manifest_features",
                              return_value=nimbus.parse_manifest(manifest)):
        return nimbus.onset_facts(SIG, "Firefox", channel, version, NODE, FRAMES,
                                  first_ever=first_ever, now=now)


class TargetingTest(unittest.TestCase):
    def test_channels(self):
        self.assertEqual(nimbus.channels_of(ROLLOUT), ["nightly"])
        self.assertEqual(nimbus.channels_of({"targeting": "browserSettings.update.channel == "
                                                          "'beta' && x"}), ["beta"])
        self.assertEqual(nimbus.channels_of({"channel": "release", "targeting": ""}),
                         ["release"])
        self.assertIsNone(nimbus.channels_of({"targeting": "region == 'US'"}))

    def test_version_compare(self):
        self.assertEqual(nimbus.version_cmp("161.0a1", "160.!"), 1)
        self.assertEqual(nimbus.version_cmp("160.0b1", "160.!"), 1)
        self.assertEqual(nimbus.version_cmp("159.0.1", "160.!"), -1)
        self.assertEqual(nimbus.version_cmp("160.0b1", "160.*"), -1)
        self.assertEqual(nimbus.version_cmp("158.4.1", "158.4.0"), 1)
        self.assertIsNone(nimbus.version_cmp("", "160.!"))

    def test_version_components_and_prerelease_suffixes(self):
        self.assertEqual(nimbus.version_cmp("160.0.1", "160.0"), 1)
        self.assertEqual(nimbus.version_cmp("160.0b9", "160.0b10"), -1)
        self.assertEqual(nimbus.version_cmp("161.0a1", "161.0"), -1)
        self.assertEqual(nimbus.version_cmp("153.4.0esr", "153.!"), 1)
        # Equivalent version spellings.
        self.assertEqual(nimbus.version_cmp("1.0", "1.0.0"), 0)
        self.assertEqual(nimbus.version_cmp("1.0+", "1.1pre"), 0)
        self.assertEqual(nimbus.version_cmp("1.1pre", "1.1pre0"), 0)
        self.assertEqual(nimbus.version_cmp("1.1pre1a", "1.1pre1"), -1)
        self.assertEqual(nimbus.version_cmp("1.1pre10a", "1.1pre10"), -1)
        self.assertEqual(nimbus.version_cmp("1.10", "1.9"), 1)
        self.assertEqual(nimbus.version_cmp("1.*", "1.9999"), 1)

    def test_targets(self):
        r = nimbus.compact(ROLLOUT)
        self.assertTrue(nimbus.targets(r, "nightly", "161.0a1"))
        self.assertFalse(nimbus.targets(r, "beta", "160.0b1"))
        # Its bound sits under `experiment.slug in activeRollouts ||`: conditional.
        self.assertTrue(nimbus.targets(r, "nightly", "159.0a1"))
        # A missing version does not exclude a recipe.
        self.assertTrue(nimbus.targets(r, "nightly", None))
        beta = nimbus.compact(dict(ROLLOUT, targeting="browserSettings.update.channel in "
                                                      "[\"aurora\"]"))
        self.assertTrue(nimbus.targets(beta, "beta", "160.0b1"))
        esr = nimbus.compact(dict(ROLLOUT, targeting="browserSettings.update.channel == \"esr\""))
        self.assertTrue(nimbus.targets(esr, "esr153", "153.4.0esr"))

    def test_only_mandatory_terms_exclude(self):
        # Both bounds are inside an enrollment OR group.
        real = nimbus.compact(dict(ROLLOUT, targeting=LAUNCH_AT_LOGIN))
        self.assertEqual((real["channels"], real["versions"]), (["release"], []))
        self.assertTrue(nimbus.targets(real, "release", "156.0"))
        # The same OR group under a mandatory bound: only 156.! binds.
        mixed = nimbus.compact(dict(ROLLOUT, targeting=(
            "(!isMSIX || os.windowsBuildNumber >= 22000 || "
            "version|versionCompare('157.!') >= 0) && version|versionCompare('156.!') >= 0")))
        self.assertEqual(mixed["versions"], [[">=", "156.!"]])
        self.assertTrue(nimbus.targets(mixed, "release", "156.0"))
        self.assertFalse(nimbus.targets(mixed, "release", "155.0"))
        either = nimbus.compact(dict(ROLLOUT, targeting=(
            "browserSettings.update.channel == \"release\" || isMSIX")))
        self.assertIsNone(either["channels"])

    def test_mandatory_terms(self):
        self.assertEqual(nimbus.mandatory_terms("a && (b || c) && !d && ((e && f))"),
                         ["a", "e", "f"])
        self.assertEqual(nimbus.mandatory_terms("x == 'p||q' && (y)"), ["x == 'p||q'", "y"])
        self.assertEqual(nimbus.mandatory_terms("(a) || (b)"), [])
        self.assertEqual(nimbus.mandatory_terms("(a) && (b)"), ["a", "b"])
        self.assertEqual(nimbus.mandatory_terms(""), [])

    def test_operators_in_escaped_strings_do_not_change_targeting(self):
        targeting = r'''(label == 'it\'s (beta||release)' && labels["a&&b"] == "x") && (
            browserSettings.update.channel in ["nightly"] &&
            version|versionCompare('160.!') >= 0)'''
        recipe = nimbus.compact(dict(ROLLOUT, targeting=targeting))
        self.assertEqual(recipe["channels"], ["nightly"])
        self.assertEqual(recipe["versions"], [[">=", "160.!"]])
        self.assertFalse(nimbus.targets(recipe, "nightly", "159.0a1"))

    def test_targets_compare_whole_versions(self):
        def bound(expr):
            return nimbus.compact(dict(ROLLOUT, targeting="(version|versionCompare('{}') {} 0)"
                                       .format(*expr)))
        self.assertTrue(nimbus.targets(bound(("161.0", "<")), "nightly", "161.0a1"))
        self.assertFalse(nimbus.targets(bound(("160.0b10", ">=")), "nightly", "160.0b9"))
        self.assertFalse(nimbus.targets(bound(("160.0", "<=")), "nightly", "160.0.1"))

    def test_compact(self):
        r = nimbus.compact(ROLLOUT)
        self.assertEqual(r["versions"], [])
        self.assertEqual(r["channels"], ["nightly"])
        self.assertEqual(r["share"], 1.0)
        self.assertEqual(r["values"], {"autofill-addresses-rust-storage":
                                       [["control", '{"enabled": true}']]})
        self.assertEqual(r["variables"], {"autofill-addresses-rust-storage": ["enabled"]})


class ManifestTest(unittest.TestCase):
    def test_parse(self):
        got = nimbus.parse_manifest(MANIFEST)
        self.assertEqual(got["autofill-addresses-rust-storage"], {
            "owners": ["dlee@mozilla.com"],
            "variables": {"enabled": ["extensions.formautofill.addresses.storage.rust.enabled"]}})
        self.assertEqual(got["newtabSponsoredStories"]["variables"],
                         {"show": ["browser.newtabpage.activity-stream.showSponsored"]})

    def test_parse_keeps_each_variable_apart(self):
        got = nimbus.parse_manifest(URLBAR_MANIFEST)
        self.assertEqual(got["urlbar"]["owners"], ["someone@mozilla.com", "other@mozilla.com"])
        self.assertEqual(got["urlbar"]["variables"], {
            "actionsOnboardingTimesToShow":
                ["browser.urlbar.quickactions.timesToShowOnboardingLabel"],
            "autoFillAdaptiveHistoryEnabled": ["browser.urlbar.autoFill.adaptiveHistory.enabled"],
            "dualPrompt.onLocationChange": ["browser.mailto.dualPrompt.onLocationChange"],
            "noPref": []})
        # Exercise varying YAML indentation.
        self.assertEqual(got["newtabPromoCard"]["variables"],
                         {"enabled": ["browser.newtabpage.promoCard.enabled"]})
        self.assertEqual(got["compressionDictionaries"]["variables"],
                         {"enableDictionaries": ["network.http.dictionaries.enable"]})


class ManifestFetchTest(unittest.TestCase):
    def test_a_refused_fetch_is_short_and_cached_briefly(self):
        with mock.patch.object(nimbus, "_cache_get", return_value=None), \
                mock.patch.object(nimbus, "_cache_set") as cache_set, \
                mock.patch.object(nimbus.hgedge, "raw_file", return_value=None) as raw:
            self.assertEqual(nimbus.manifest_features(NODE, "nightly"), {})
        self.assertEqual(raw.call_args.kwargs["retries"], nimbus._MANIFEST_TRIES)
        cache_set.assert_called_once_with("manifest:" + NODE, {}, nimbus._MANIFEST_MISS_TTL_S)


class TermsTest(unittest.TestCase):
    WORDS = nimbus.terms("autofill-addresses-rust-storage") | nimbus.terms(
        "extensions.formautofill.addresses.storage.rust.enabled")

    def test_panic_and_std_frames_match_nothing(self):
        self.assertEqual(nimbus.shared_terms(self.WORDS, nimbus.stack_terms(FRAMES[:4], 10)), [])

    def test_shared_terms(self):
        self.assertEqual(nimbus.shared_terms(self.WORDS, nimbus.stack_terms(FRAMES, 10)),
                         ["address", "autofill"])

    def test_stack_frame_limit(self):
        self.assertNotIn("autofill", nimbus.stack_terms(FRAMES, 2))


class OnsetFactsTest(unittest.TestCase):
    def test_motivating_case(self):
        got = _facts([ROLLOUT, UNRELATED, RELEASE_ONLY, AFTER_ONSET, TOO_OLD])
        self.assertEqual(got["status"], "ok")
        self.assertEqual([r["slug"] for r in got["recipes"]], [ROLLOUT["slug"]])
        rec = got["recipes"][0]
        self.assertEqual(rec["minutes_before"], 26)
        self.assertEqual(rec["shared"], ["address", "autofill"])
        self.assertEqual(rec["features"][0]["owners"], ["dlee@mozilla.com"])
        self.assertEqual(got["others"], [UNRELATED["slug"]])

    def test_statuses(self):
        self.assertEqual(_facts([RELEASE_ONLY])["status"], "none")
        self.assertEqual(_facts([ROLLOUT], first=None)["status"], "failed")
        self.assertEqual(_facts(None)["status"], "failed")
        old = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.assertEqual(_facts([ROLLOUT], first=old)["status"], "old")
        # Quiet for longer than the window, then back: not an onset.
        self.assertEqual(_facts([ROLLOUT], first_ever="2026-08-01")["status"], "old")
        self.assertEqual(_facts([ROLLOUT], first_ever="2026-10-27")["status"], "ok")
        with mock.patch.object(nimbus.config, "get_agent_nimbus_onset",
                               return_value=dict(nimbus.config.get_agent_nimbus_onset(),
                                                 enabled=False)):
            self.assertEqual(_facts([ROLLOUT])["status"], "disabled")
        self.assertEqual(nimbus.onset_facts(SIG, "Thunderbird", "nightly", "161.0a1", NODE,
                                            FRAMES, now=NOW)["status"], "unsupported")

    def test_a_renamed_crash_starts_under_its_old_name(self):
        family = {"signature_predecessors": [
            {"signature": "old::Name", "first_seen_ever_date": "2025-03-10"},
            {"signature": "other::Name", "first_seen_ever_date": None}, "not-a-row"]}
        first_ever = orch._onset_first_ever("2026-10-27", family)
        self.assertEqual(first_ever, "2025-03-10")
        self.assertEqual(_facts([ROLLOUT], first_ever=first_ever)["status"], "old")
        # No predecessor, or a failed family lookup: the signature's own day.
        self.assertEqual(orch._onset_first_ever("2026-10-27", {}), "2026-10-27")
        self.assertEqual(orch._onset_first_ever(None, {"signature_family_lookup": "failed"}),
                         None)

    def test_first_report_retried_once(self):
        got = _facts([ROLLOUT], first_exc=[OSError("IncompleteRead"), FIRST])
        self.assertEqual(got["status"], "ok")
        got = _facts([ROLLOUT], first_exc=[OSError("a"), OSError("b")])
        self.assertEqual(got["status"], "failed")

    def test_no_manifest_still_matches_on_feature_id(self):
        got = _facts([ROLLOUT], manifest="")
        self.assertEqual(got["recipes"][0]["shared"], ["address", "autofill"])
        self.assertEqual(got["recipes"][0]["features"][0]["prefs"], [])


class VariablePrefsTest(unittest.TestCase):
    """Only configured variables contribute prefs."""

    def _facts(self, frames):
        with mock.patch.object(nimbus, "first_report", return_value=FIRST), \
                mock.patch.object(nimbus, "live_recipes",
                                  return_value=[nimbus.compact(ADAPTIVE_AUTOFILL)]), \
                mock.patch.object(nimbus, "manifest_features",
                                  return_value=nimbus.parse_manifest(URLBAR_MANIFEST)):
            return nimbus.onset_facts(SIG, "Firefox", "nightly", "161.0a1", NODE, frames,
                                      now=NOW)

    def test_another_variables_pref_does_not_match(self):
        frames = [{"filename": "browser/modules/QuickActions.cpp",
                   "function": "QuickActions::Query", "node": NODE}]
        got = self._facts(frames)
        self.assertEqual(got["recipes"], [])
        self.assertEqual(got["others"], [ADAPTIVE_AUTOFILL["slug"]])

    def test_only_the_configured_variables_prefs_are_listed(self):
        frames = [{"filename": "toolkit/places/AdaptiveHistory.cpp",
                   "function": "places::AdaptiveHistory::Update", "node": NODE}]
        got = self._facts(frames)
        self.assertEqual(got["recipes"][0]["features"][0]["prefs"],
                         ["browser.urlbar.autoFill.adaptiveHistory.enabled"])
        d = Dossier(crash={"uuid": "u", "signature": SIG, "frames": []})
        orch._record_nimbus_onset(d, {"nimbus_onset": got})
        note = report_bug.build_nimbus_onset_note(d.corroborations)
        self.assertIn("`browser.urlbar.autoFill.adaptiveHistory.enabled`", note)
        self.assertNotIn("quickactions", note)

    def test_auto_prefix_is_not_a_term(self):
        self.assertNotIn("auto", nimbus.terms("browser.urlbar.autoFill.adaptiveHistory"))
        self.assertNotIn("auto", nimbus.terms("mozilla::dom::AutoEntryScript"))


class OwnerRoutingTest(unittest.TestCase):
    """Actionable filings prefer an eligible matching feature owner."""

    DIMI = {"nick": "dimi", "name": "", "email": "dlee@mozilla.com",
            "account": "dlee@mozilla.com", "account_name": "Dimi Lee [:dimi]"}
    ASK = ":dimi, as the owner of the Nimbus feature `autofill-addresses-rust-storage`, can you "
    ASK += "have a look please?"

    def _onset(self, minutes=26, owners=("team@mozilla.com", "dlee@mozilla.com")):
        return {"first_report": "2026-10-27T17:42:40+00:00", "recipes": [{
            "slug": ROLLOUT["slug"], "rollout": True, "published": ROLLOUT["publishedDate"],
            "minutes_before": minutes, "features": ["autofill-addresses-rust-storage"],
            "prefs": ["extensions.formautofill.addresses.storage.rust.enabled"],
            "owners": {"autofill-addresses-rust-storage": list(owners)},
            "shared": ["address", "autofill"]}]}

    def _owner(self, onset, users, away=False):
        with mock.patch.object(report_bug, "_bugzilla_user",
                               side_effect=lambda e: users.get(e, {"exists": False})), \
                mock.patch.object(report_bug, "_person_for_account",
                                  side_effect=lambda e: dict(self.DIMI) if e == "dlee@mozilla.com"
                                  else {}), \
                mock.patch.object(report_bug, "_away", return_value=away):
            return report_bug._nimbus_owner_person({"nimbus_onset": onset})

    def test_a_team_alias_with_no_account_is_skipped(self):
        users = {"team@mozilla.com": {"exists": False}, "dlee@mozilla.com": {"exists": True}}
        got = self._owner(self._onset(), users)
        self.assertEqual((got["person"]["account"], got["feature"]),
                         ("dlee@mozilla.com", "autofill-addresses-rust-storage"))

    def test_who_is_not_asked(self):
        ok = {"dlee@mozilla.com": {"exists": True}}
        self.assertIsNone(self._owner(self._onset(minutes=3 * 24 * 60), ok), "too early")
        self.assertIsNone(self._owner(self._onset(), ok, away=True), "away")
        self.assertIsNone(self._owner(self._onset(), {"dlee@mozilla.com": {"unverified": True}}),
                          "BMO unreachable")
        self.assertIsNone(self._owner(self._onset(owners=()), ok), "no owner")
        self.assertIsNone(report_bug._nimbus_owner_person({}), "no onset")

    def _preview(self, decision="actionable", minutes=26):
        d = tav._preview_dossier(nimbus_onset=self._onset(minutes=minutes))
        d["verdict"]["decision"] = decision
        with mock.patch.object(report_bug, "_person_for_account",
                               side_effect=lambda e: dict(self.DIMI) if e == "dlee@mozilla.com"
                               else {}):
            return tav._build_preview(d)

    def test_an_actionable_filing_asks_the_feature_owner(self):
        p = self._preview()
        self.assertEqual((p["needinfo_email"], p["needinfo"]), ("dlee@mozilla.com", self.ASK))
        self.assertIn("Nimbus recipes that went live shortly before", p["comment"])
        self.assertIn(self.ASK, p["comment"])
        self.assertIn("(bug 2010557) by :bobowen.", p["comment"])

    def test_otherwise_the_author_is_asked(self):
        for name, kw in (("recipe too early", {"minutes": 3 * 24 * 60}),
                         ("a lead names its changeset", {"decision": "lead"})):
            with self.subTest(name):
                p = self._preview(**kw)
                self.assertEqual((p["needinfo_email"], p["needinfo"]),
                                 ("bob@x.com", ":bobowen, can you have a look please?"))


class MultiFeatureTest(unittest.TestCase):
    """Route through the matching feature in a recipe with several owners."""

    MANIFEST = (
        "adsBackend:\n  owner: efixler@mozilla.com\n  variables:\n    flags:\n"
        "      type: json\n"
        "newtabSponsoredContent:\n  owner: a@mozilla.com\n  variables:\n    tilesCounts:\n"
        "      type: string\n"
        "newtabTrainhop:\n  owner: mconley@mozilla.com\n  variables:\n    payload:\n"
        "      type: json\n    type:\n      type: string\n")
    RECIPE = {
        "slug": "relevant-sponsored-stories-on-nightly", "isRollout": False, "channel": "",
        "featureIds": ["adsBackend", "newtabSponsoredContent", "newtabTrainhop"],
        "publishedDate": "2026-10-27T16:47:56Z",
        "targeting": "(browserSettings.update.channel in [\"nightly\"])",
        "bucketConfig": {"count": 10000, "total": 10000},
        "branches": [{"slug": "treatment", "features": [
            {"featureId": "adsBackend", "value": {"flags": {}}},
            {"featureId": "newtabSponsoredContent", "value": {"tilesCounts": "1"}},
            {"featureId": "newtabTrainhop", "value": {"payload": {}, "type": "x"}}]}],
    }
    FRAMES = [{"filename": "browser/modules/Trainhop.cpp", "function": "Trainhop::Fetch",
               "node": NODE}]

    def _facts(self):
        with mock.patch.object(nimbus, "first_report", return_value=FIRST), \
                mock.patch.object(nimbus, "live_recipes",
                                  return_value=[nimbus.compact(self.RECIPE)]), \
                mock.patch.object(nimbus, "manifest_features",
                                  return_value=nimbus.parse_manifest(self.MANIFEST)):
            return nimbus.onset_facts(SIG, "Firefox", "nightly", "161.0a1", NODE, self.FRAMES,
                                      now=NOW)

    def test_the_matching_feature_is_kept_apart(self):
        rec = self._facts()["recipes"][0]
        self.assertEqual([(f["id"], f["shared"]) for f in rec["features"]],
                         [("adsBackend", []), ("newtabSponsoredContent", []),
                          ("newtabTrainhop", ["trainhop"])])
        self.assertEqual(rec["shared"], ["trainhop"])

    def test_the_matching_features_owner_is_asked(self):
        d = Dossier(crash={"uuid": "u", "signature": SIG, "frames": []})
        orch._record_nimbus_onset(d, {"nimbus_onset": self._facts()})
        rec = d.corroborations["nimbus_onset"]["recipes"][0]
        self.assertEqual((rec["features"], rec["owners"]),
                         (["newtabTrainhop"], {"newtabTrainhop": ["mconley@mozilla.com"]}))
        mconley = {"nick": "mconley", "account": "mconley@mozilla.com"}
        with mock.patch.object(report_bug, "_bugzilla_user", return_value={"exists": True}), \
                mock.patch.object(report_bug, "_person_for_account",
                                  side_effect=lambda e: dict(mconley, email=e, account=e)), \
                mock.patch.object(report_bug, "_away", return_value=False):
            got = report_bug._nimbus_owner_person(d.corroborations)
        self.assertEqual((got["person"]["account"], got["feature"]),
                         ("mconley@mozilla.com", "newtabTrainhop"))
        note = report_bug.build_nimbus_onset_note(d.corroborations)
        self.assertIn("Feature `newtabTrainhop` shares `trainhop`", note)
        self.assertNotIn("adsBackend", note)

    def test_the_brief_details_the_matching_feature(self):
        text = "\n".join(triage._nimbus_onset_lines({"nimbus_onset": self._facts()}))
        self.assertIn("feature newtabTrainhop; owner mconley@mozilla.com", text)
        self.assertIn("Also sets, sharing no term: adsBackend, newtabSponsoredContent.", text)
        self.assertNotIn("efixler", text)


class SurfacesTest(unittest.TestCase):
    def test_brief_lines(self):
        lines = triage._nimbus_onset_lines({"nimbus_onset": _facts([ROLLOUT, UNRELATED])})
        text = "\n".join(lines)
        self.assertIn("addresses-storage-migration-nightly-rollout: rollout to 100% of eligible "
                      "clients, live 2026-10-27 17:16 UTC, 26 minutes before", text)
        self.assertIn("sets extensions.formautofill.addresses.storage.rust.enabled", text)
        self.assertIn("Shares with the stack: address, autofill.", text)
        self.assertIn("sharing no term with the stack: relevant-sponsored-stories-on-nightly",
                      text)

    def test_brief_lines_without_a_match(self):
        none = triage._nimbus_onset_lines({"nimbus_onset": _facts([RELEASE_ONLY])})
        self.assertTrue(none[1].startswith("NIMBUS: no live firefox-desktop rollout"))
        others = triage._nimbus_onset_lines({"nimbus_onset": _facts([UNRELATED])})
        self.assertIn("none shares a term with the top of this stack", others[1])
        self.assertEqual(triage._nimbus_onset_lines({}), [])
        self.assertEqual(triage._nimbus_onset_lines(
            {"nimbus_onset": _facts([ROLLOUT], first=None)}), [])

    def test_recorder_and_bug_note(self):
        d = Dossier(crash={"uuid": "u", "signature": SIG, "frames": []})
        orch._record_nimbus_onset(d, {"nimbus_onset": _facts([ROLLOUT, UNRELATED])})
        rec = d.corroborations["nimbus_onset"]["recipes"]
        self.assertEqual(rec[0]["prefs"],
                         ["extensions.formautofill.addresses.storage.rust.enabled"])
        note = report_bug.build_nimbus_onset_note(d.corroborations)
        self.assertIn("- Rollout [`addresses-storage-migration-nightly-rollout`](https://"
                      "experimenter.services.mozilla.com/nimbus/addresses-storage-migration-"
                      "nightly-rollout/summary) went live on 2026-10-27 17:16 UTC, 26 minutes "
                      "before the first report.", note)
        self.assertIn("shares `address`, `autofill` with the top of the stack.", note)
        self.assertNotIn("relevant-sponsored", note)

    def test_recorder_ignores_unmatched(self):
        d = Dossier(crash={"uuid": "u", "signature": SIG, "frames": []})
        orch._record_nimbus_onset(d, {"nimbus_onset": _facts([UNRELATED])})
        self.assertNotIn("nimbus_onset", d.corroborations or {})
        orch._record_nimbus_onset(d, {})
        self.assertNotIn("nimbus_onset", d.corroborations or {})

    def test_bug_note_hour_bound(self):
        c = {"nimbus_onset": {"recipes": [{"slug": "s", "rollout": True,
                                           "published": "2026-10-24T17:00:00Z",
                                           "minutes_before": 3 * 24 * 60, "features": ["glean"],
                                           "prefs": [], "shared": ["glean"]}]}}
        self.assertEqual(report_bug.build_nimbus_onset_note(c), "")
        c["nimbus_onset"]["recipes"][0]["minutes_before"] = 47 * 60
        self.assertIn("47 hours before", report_bug.build_nimbus_onset_note(c))


if __name__ == "__main__":
    unittest.main()
