# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The other-application map (`config._OTHER_APP_PRODUCTS`) and the two things that used to be
# hand-written copies of it: the agent-facing prose in `agent/tools/bugzilla.py` and
# `eval/study_corpus._NON_DESKTOP_PRODUCTS`. Plus `bin/audit_products.py`, whose LOGIC is tested
# here on captured 2026-08-21 snapshots — the script itself talks to live BMO + Socorro and is
# deliberately not in this suite, nor in the release phase, nor in predeploy (its docstring says
# why for each).
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_other_app_products
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import importlib.util  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config  # noqa: E402
from crashclouseau.agent.tools import bugzilla as bugzilla_tools  # noqa: E402
from crashclouseau.eval import study_corpus  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "audit_products", os.path.join(_HERE, "..", "bin", "audit_products.py")
)
audit_products = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_products)

# Socorro `_facets=product`, no product filter, the 30 days to 2026-08-21. Re-measure with
# `uv run python bin/audit_products.py`; the counts move daily, the SET is what is claimed.
_SOCORRO_30D = {
    "Firefox": 1129749, "Fenix": 458043, "Thunderbird": 223861,
    "Focus": 5804, "ReferenceBrowser": 125,
}
# BMO /rest/product?type=accessible on 2026-08-21, trimmed to what this file touches. `Fenix` and
# `Reference Browser` are absent because BMO has no products by those names.
_BMO = {
    "Thunderbird": True, "MailNews Core": True, "Calendar": True, "Chat Core": True,
    "SeaMonkey": True, "Firefox": True, "Core": True, "Toolkit": True,
    "Firefox for Android": True, "GeckoView": True, "Focus": True, "Mozilla VPN": True,
    "Plugins Graveyard": False,
}


def _signature_bugs_description():
    return next(t.description for t in bugzilla_tools.TOOLS if t.name == "signature_bugs")


class TestTheAuditOfTheMap(unittest.TestCase):
    """`bin/audit_products.py` — the completeness claim, executable."""

    def test_it_fails_today_and_names_exactly_what_is_missing(self):
        # The whole finding, self-reporting: three applications report to Socorro with no entry
        # in the map, and one entry reports nothing. Kept as a test so that the day somebody
        # "fixes" the map, this snapshot says what the map used to be wrong about.
        result = audit_products.audit(_BMO, _SOCORRO_30D, ours=["Firefox"])
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["inactive"], [])
        self.assertEqual([p for p, _ in result["unmapped"]],
                         ["Fenix", "Focus", "ReferenceBrowser"])
        self.assertEqual(result["silent"], ["SeaMonkey"])
        self.assertFalse(audit_products.is_clean(result))

    def test_the_obvious_wrong_entry_is_caught_because_bmo_has_no_fenix(self):
        # The repair this check exists to stop. `Fenix` looks like a product and is a search
        # alias: on 2026-08-21 `?product=Fenix` returned 21,891 bugs and every one of them read
        # `product: "Firefox for Android"`, while `/rest/product?names=Fenix` returned nothing.
        result = audit_products.audit(_BMO, _SOCORRO_30D,
                                      mapped={"Fenix": ["Fenix"]}, ours=["Firefox"])
        self.assertEqual(result["unknown"], [("Fenix", "Fenix")])

    def test_a_retired_product_is_caught(self):
        result = audit_products.audit(_BMO, _SOCORRO_30D,
                                      mapped={"Plugins": ["Plugins Graveyard"]},
                                      ours=["Firefox"])
        self.assertEqual(result["inactive"], [("Plugins", "Plugins Graveyard")])

    def test_it_is_not_a_constant_failure(self):
        # A FAIL has to mean something, so a map that does name every reporting application
        # passes. (This is the Fenix-day map's shape, not a recommendation — see the evidence
        # block above `config._OTHER_APP_PRODUCTS` for why the entries are not there today.)
        mapped = {
            "Thunderbird": ["Thunderbird", "MailNews Core", "Calendar", "Chat Core"],
            "SeaMonkey": ["SeaMonkey"], "Fenix": ["Firefox for Android"],
            "Focus": ["Focus"], "ReferenceBrowser": ["GeckoView"],
        }
        socorro = dict(_SOCORRO_30D, SeaMonkey=1)
        result = audit_products.audit(_BMO, socorro, mapped=mapped, ours=["Firefox"])
        self.assertTrue(audit_products.is_clean(result), result)

    def test_there_is_no_volume_floor(self):
        # The claim audited is "reports to Socorro at all", so one report is a violation. A floor
        # would be a threshold fit on nothing — ReferenceBrowser's 125 is not special, and the
        # printed counts are what lets a reader price an entry.
        result = audit_products.audit(_BMO, {"Firefox": 10, "Newapp": 1}, ours=["Firefox"])
        self.assertEqual(result["unmapped"], [("Newapp", 1)])

    def test_a_product_we_triage_is_never_flagged(self):
        # config.products is the other half of the predicate: on Fenix day the check must go
        # quiet about Fenix by itself, not need a second edit.
        result = audit_products.audit(_BMO, _SOCORRO_30D, ours=["Firefox", "Fenix"])
        self.assertNotIn("Fenix", [p for p, _ in result["unmapped"]])

    def test_the_shipped_map_is_the_default(self):
        result = audit_products.audit(_BMO, _SOCORRO_30D, ours=["Firefox"])
        self.assertEqual(result["silent"], ["SeaMonkey"])
        self.assertEqual(
            sorted(p for products in config._OTHER_APP_PRODUCTS.values() for p in products),
            ["Calendar", "Chat Core", "MailNews Core", "SeaMonkey", "Thunderbird"])


class TestTheAgentProseIsRenderedFromTheMap(unittest.TestCase):
    """`agent/tools/bugzilla.py:signature_bugs` was a second hand-written copy of the map."""

    def test_no_placeholder_survives_import(self):
        self.assertNotIn("{other_applications}", _signature_bugs_description())

    def test_every_application_and_product_in_the_map_is_named(self):
        description = _signature_bugs_description()
        for app, products in config._OTHER_APP_PRODUCTS.items():
            self.assertIn(app, description)
            for product in products:
                self.assertIn(product, description)

    def test_the_clause_follows_the_map_without_a_second_edit(self):
        # The rendered description is built at import, so this pins the renderer rather than the
        # already-rendered string: adding an entry has to reach the agent from one edit.
        with mock.patch.dict(config._OTHER_APP_PRODUCTS, {"Focus": ["Focus"]}, clear=False):
            self.assertIn("Focus", config.describe_other_applications())

    def test_the_crashing_application_is_left_out_and_none_leaves_out_nobody(self):
        self.assertNotIn("MailNews Core", config.describe_other_applications("Thunderbird"))
        self.assertIn("MailNews Core", config.describe_other_applications())
        self.assertIn("MailNews Core", config.describe_other_applications("Firefox"))

    def test_a_single_eponymous_product_is_not_parenthesised(self):
        with mock.patch.object(config, "_OTHER_APP_PRODUCTS", {"SeaMonkey": ["SeaMonkey"]}):
            self.assertEqual(config.describe_other_applications(), "SeaMonkey")
        with mock.patch.object(config, "_OTHER_APP_PRODUCTS", {}):
            self.assertEqual(config.describe_other_applications(), "")


class TestTheThirdCopyIsDerived(unittest.TestCase):
    """`eval/study_corpus._NON_DESKTOP_PRODUCTS` disagreed with the map on 26 of 287 fixtures."""

    def test_it_covers_every_product_config_calls_foreign(self):
        # The invariant that FAILED before this change: Calendar, Chat Core and SeaMonkey were
        # foreign to config and triageable to the study corpus, in the same repo, untested.
        foreign = {p.lower() for p in config.get_other_app_products("Firefox")}
        self.assertTrue(study_corpus._NON_DESKTOP_PRODUCTS >= foreign,
                        foreign - study_corpus._NON_DESKTOP_PRODUCTS)

    def test_the_android_family_is_still_dropped(self):
        # Kept a literal on purpose: config's map answers "whose venue is this bug" and keeps
        # Fenix/GeckoView on the Firefox side, which is not the same question as "can Clouseau
        # triage this crash".
        for product in ("Firefox for Android", "Fenix", "FennecAndroid", "GeckoView"):
            self.assertFalse(study_corpus._is_target_crash({"product": product}), product)

    def test_the_study_corpus_itself_is_not_eaten(self):
        # The counter-example. None of these products is exclusive to any application, so no
        # widening of the foreign list may reach them. Counts are the 287-fixture census.
        for product, _ in (("Core", 238), ("Toolkit", 10), ("Firefox", 7),
                           ("External Software Affecting Firefox", 2),
                           ("Application Services", 1)):
            self.assertTrue(study_corpus._is_target_crash({"product": product}), product)

    def test_the_split_over_the_287_fixture_census_is_unchanged(self):
        # spike/regressor_dataset_blind, 2026-08-21. The literal dropped 29; so does the derived
        # union, because the three products it adds have no fixture. 0 fixtures moved.
        census = {"Core": 238, "Firefox for Android": 25, "Toolkit": 10, "Firefox": 7,
                  "External Software Affecting Firefox": 2, "MailNews Core": 2,
                  "Application Services": 1, "GeckoView": 1, "Thunderbird": 1}
        dropped = sum(n for product, n in census.items()
                      if not study_corpus._is_target_crash({"product": product}))
        self.assertEqual((sum(census.values()), dropped), (287, 29))

    def test_a_java_signature_is_still_dropped_whatever_the_product(self):
        self.assertFalse(study_corpus._is_target_crash(
            {"product": "Core", "signature": "java.lang.IllegalStateException"}))


if __name__ == "__main__":
    unittest.main()
