# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Fenix (Firefox for Android) nightly: the shipped configuration and the scaffolding it rides on.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_fenix_product

A SECOND PRODUCT on a channel label the pipeline already runs (``nightly``), which is what makes
every product-blind accessor a hazard rather than a gap: before plans/16 §13 landed, a Fenix
nightly crash inherited Firefox nightly's ARMED filing policy (comment mode, cap 10), Firefox
nightly's calibration table (published in the filed bug as "N% worth investigating"), a
``protos`` cap of 1 by silent default, a proto-cluster shared with desktop, and BMO ``Firefox``
(desktop) as a venue. The decisions D1-D19 of plans/16 §13.1 are each one entry in
``config/global.json`` or one argument on an accessor, and this module pins them the way
tests/test_esr_channel.py pinned the ESR line and tests/test_shipped_channels.py pinned beta:
the VALUE, with the measurement that chose it in the docstring, never the prose.

No test here reaches the network. The sqlite-runnable half of the Postgres story (the lenient
product type) is here; the enum widening on a long-lived Postgres is in
tests/test_enum_migration_pg.py.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from dateutil.relativedelta import relativedelta  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from crashclouseau import (  # noqa: E402
    buildsource, config, datacollector as dc, db, models, update, utils,
)

_UTC = timezone.utc

# The twelve product-keyed selector blocks (two thresholds, ten spike knobs) and the value each
# ships for Fenix nightly. Firefox nightly's numbers except where the docstrings below say why.
_FENIX_NIGHTLY = {
    ("thresholds", "installs"): 1,
    ("thresholds", "protos"): 5,
    ("spike", "floor"): 3,
    ("spike", "ratio"): 3,
    ("spike", "mature_after_days"): 5,
    ("spike", "history_days"): 21,
    ("spike", "mature_installs"): 4,
    ("spike", "min_build_installs"): 0,
    ("spike", "rising_per_day"): 0,
    ("spike", "rising_protos"): 3,
    ("spike", "real_installs"): 3,
    ("spike", "real_alert_rate"): 0.00015,
}


def _knob(block, typ, product, channel):
    if block == "thresholds":
        return config.get_threshold(typ, product, channel)
    return config.get_spike(typ, product, channel)


class _FakeQuery:
    """A stand-in for ``db.session.query(...)`` that records every ``filter`` clause and answers
    nothing, so a query that ends in ``.all()`` / ``.scalar()`` / iteration can be READ without
    a database: the product clause is asserted on the SQL the ORM would have sent."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.filters = []
        self.joins = 0

    def __getattr__(self, name):          # select_from, outerjoin, order_by, limit, ...
        return lambda *a, **k: self

    def join(self, *a, **k):
        self.joins += 1
        return self

    def filter(self, *clauses):
        self.filters.extend(clauses)
        return self

    def all(self):
        return []

    def scalar(self):
        return 0

    def first(self):
        return None

    def __iter__(self):
        return iter(self.rows)

    @property
    def sql(self):
        return " ".join(str(c) for c in self.filters)


class TestTheConfiguredProduct(unittest.TestCase):
    """D1-D3: three lists that used to be one. ``products`` defines the enum (every product
    ever contemplated), ``ingest_products`` what the tick ingests, ``agent.products`` what the
    agent spends on -- the same split ``channels`` / ``INGEST_CHANNELS`` / ``agent.channels``
    already has, because a list that defines an enum is the wrong list to default an action
    to (tests/test_shipped_channels.py::test_ingest_channels_must_always_be_set_explicitly)."""

    def test_fenix_is_a_configured_product_and_nightly_only(self):
        self.assertEqual(config.get_products(), ["Firefox", "Fenix"])
        self.assertEqual(config.get_product_channels("Fenix"), ["nightly"])
        # Firefox has no `product_channels` entry and keeps every channel -- the lever restricts
        # only what it names, so adding it changed nothing for desktop.
        self.assertEqual(config.get_product_channels("Firefox"), config.get_channels())
        self.assertEqual(config.get_product_channels("Nonesuch"), config.get_channels())
        self.assertIsNot(config.get_product_channels("Firefox"), config.get_channels())

    def test_the_ingestion_lever_defaults_to_the_config_and_the_env_overrides_it(self):
        """D2: JSON default + env override (``get_agent_channels``' shape), NOT fail-closed like
        ``INGEST_CHANNELS``. A two-entry list introduced by a deploy must not stop desktop
        ingestion for a tick because nobody set a new variable first; set-but-empty still means
        NOTHING, with a warning, so the variable is a real kill switch."""
        env = {k: v for k, v in os.environ.items() if k != "INGEST_PRODUCTS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.get_ingest_products(), ["Firefox", "Fenix"])
        for value, expected in (("Firefox", ["Firefox"]), ("Fenix", ["Fenix"]),
                                ("Fenix Firefox", ["Fenix", "Firefox"]), ("  Fenix ", ["Fenix"])):
            with self.subTest(INGEST_PRODUCTS=value), \
                    mock.patch.dict(os.environ, {"INGEST_PRODUCTS": value}):
                self.assertEqual(config.get_ingest_products(), expected)
        for value in ("", "   "):
            with self.subTest(INGEST_PRODUCTS=value), \
                    mock.patch.dict(os.environ, {"INGEST_PRODUCTS": value}), \
                    self.assertLogs(level="WARNING") as logs:
                self.assertEqual(config.get_ingest_products(), [])
            self.assertTrue(any("NO product will be ingested" in line for line in logs.output))
        # Restricted to `get_products()`: a product the enum does not know cannot be ingested
        # (the env var is not a way around the DDL), and a mocked `get_products` keeps ruling
        # the tests that mock it (tests/test_update.py).
        with mock.patch.dict(os.environ, {"INGEST_PRODUCTS": "Focus Fenix"}):
            self.assertEqual(config.get_ingest_products(), ["Fenix"])
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, "get_products", return_value=["Firefox"]):
            self.assertEqual(config.get_ingest_products(), ["Firefox"])

    def test_the_agent_products_lever_and_its_kill_switch(self):
        """D3: Fenix IS triaged from day one (~11-12 native pairs/day + a few Java, bounded by
        ``thresholds.protos`` 5 and the per-(channel, product) cluster dedup).
        ``AGENT_PRODUCTS=Firefox`` is the deploy-free way to stop that spend: Fenix nightly and
        Firefox nightly share the label ``nightly``, so ``AGENT_CHANNELS`` cannot stop one
        without the other."""
        env = {k: v for k, v in os.environ.items() if k != "AGENT_PRODUCTS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.get_agent_products(), ["Firefox", "Fenix"])
        with mock.patch.dict(os.environ, {"AGENT_PRODUCTS": "Firefox"}):
            self.assertEqual(config.get_agent_products(), ["Firefox"])
        with mock.patch.dict(os.environ, {"AGENT_PRODUCTS": ""}), \
                self.assertLogs(level="WARNING") as logs:
            self.assertEqual(config.get_agent_products(), [])
        self.assertTrue(any("NO product will be triaged" in line for line in logs.output))
        # The JSON key is the real one (no schema validation exists to catch a typo).
        self.assertEqual(config.get_agent()["products"], ["Firefox", "Fenix"])


class TestTheShippedFenixNightlyKnobs(unittest.TestCase):
    """What Fenix nightly's selector can see: twelve product-keyed blocks, each with a Fenix
    entry, because ``get_threshold`` / ``get_spike`` fall back SILENTLY for an unknown product
    (threshold -> 1 for installs AND protos, spike -> ``_SPIKE_DEFAULTS``). ``protos`` 1 was
    the only spend bound Fenix had before these entries existed."""

    def test_every_product_keyed_block_names_fenix(self):
        """The forcing function: a thirteenth block added without a Fenix entry falls back to
        the code default on Fenix alone, and nothing else in the suite would say so."""
        g = config._get_global()
        blocks = [("thresholds", t) for t in g["thresholds"]] + [("spike", t) for t in g["spike"]]
        self.assertEqual(sorted(blocks), sorted(_FENIX_NIGHTLY), "a new product-keyed block: "
                         "decide its Fenix value and add it to _FENIX_NIGHTLY")
        for block, typ in blocks:
            with self.subTest(block=block, typ=typ):
                entry = g[block][typ]
                self.assertIn("Fenix", entry)
                # Nightly ONLY: the pairing lever keeps Fenix off beta/release/esr153, and an
                # entry there would be a number nobody measured.
                self.assertEqual(set(entry["Fenix"]), {"nightly"})

    def test_the_shipped_values(self):
        """Firefox nightly's numbers, with three DELIBERATE departures.

        ``protos`` 5, not nightly's 50. Nightly's 50 never binds (mean 1.07 protos per selected
        pair); on beta the cap is the DOMINANT cost term (4 pairs -> 37 protos), and 5 is the
        value priced there on a live selection (cap 1 -> 4 runs, 3 -> 12, 5 -> 20, 10 -> 35,
        50 -> 37). Fenix stacks are unmeasured on this axis, so the priced cap is the starting
        cost decision (plans/16 §11.2), not a fit.

        ``rising_per_day`` 0: the rate path is OFF. It is a BUDGET over the ``sigtrend`` rollup,
        which has no Fenix history yet -- a rank over an empty series is a rank over noise.

        ``mature_installs`` 4 and ``real_installs`` 3 are nightly's and must not be LOWERED:
        ``cardinality_install_time`` is a weak machine proxy on Android (51.6% of native
        reports carry an install_time before 2010; 14.6% span more than one buildid), and the
        install bar is the gate that removes the A95XF4 device-farm ProviderException family
        (99.7% one android_model) -- plans/16 §2.

        ``installs`` 1, ``floor`` 3, ``ratio`` 3, ``mature_after_days`` 5, ``history_days`` 21,
        ``min_build_installs`` 0, ``rising_protos`` 3, ``real_alert_rate`` 0.00015: nightly's,
        because Fenix nightly is a nightly -- one build a day, no merge-day build, every build
        from the ``builds`` table rather than ``get_last_versions``."""
        for (block, typ), value in _FENIX_NIGHTLY.items():
            with self.subTest(block=block, typ=typ):
                self.assertEqual(_knob(block, typ, "Fenix", "nightly"), value)
        # The departures, as relations and not only literals.
        self.assertLess(config.get_threshold("protos", "Fenix", "nightly"),
                        config.get_threshold("protos", "Firefox", "nightly"))
        self.assertEqual(config.get_threshold("protos", "Fenix", "nightly"),
                         config.get_threshold("protos", "Firefox", "beta"))
        self.assertEqual(config.get_spike("rising_per_day", "Fenix", "nightly"), 0)
        self.assertGreater(config.get_spike("rising_per_day", "Firefox", "nightly"), 0)
        for typ in ("mature_installs", "real_installs"):
            self.assertGreaterEqual(config.get_spike(typ, "Fenix", "nightly"),
                                    config.get_spike(typ, "Firefox", "nightly"), typ)
        # Everything else is Firefox nightly's, so the two cannot drift apart unnoticed.
        for (block, typ), value in _FENIX_NIGHTLY.items():
            if typ not in ("protos", "rising_per_day"):
                self.assertEqual(value, _knob(block, typ, "Firefox", "nightly"), typ)
        # Firefox's values are byte-identical to what they were.
        self.assertEqual(config.get_threshold("installs", "Firefox", "nightly"), 1)
        self.assertEqual(config.get_threshold("protos", "Firefox", "nightly"), 50)
        self.assertEqual(config.get_threshold("installs", "Firefox", "beta"), 6)

    def test_the_channel_branching_consumers_treat_fenix_nightly_as_a_nightly(self):
        """``get_maturity_bar`` and ``get_no_user_build_floor`` branch on ``channel ==
        "nightly"`` only, so Fenix nightly reads the maturity bar and floor 0 exactly as
        Firefox nightly does -- the install half of the bar being the one plans/16 flags as
        weak on Android, which is why its value is pinned above and not lowered."""
        self.assertEqual(dc.get_maturity_bar("Fenix", "nightly"),
                         dc.get_maturity_bar("Firefox", "nightly"))
        self.assertEqual(dc.get_maturity_bar("Fenix", "nightly"), (5, 4))
        self.assertEqual(dc.get_no_user_build_floor("Fenix", "nightly"), 0)
        # The floor sits above the install threshold, so it BINDS (tests/test_selection_log.py
        # explains why a floor at or below the threshold is not a lever).
        self.assertGreater(config.get_spike("floor", "Fenix", "nightly"),
                           config.get_threshold("installs", "Fenix", "nightly"))

    def test_an_unpaired_channel_falls_to_the_code_default(self):
        """No Fenix beta entry, by design: what a stray ``get_spike("floor", "Fenix", "beta")``
        would read is the quiet code default, not Firefox beta's number."""
        self.assertEqual(config.get_spike("floor", "Fenix", "beta"), config._SPIKE_DEFAULTS["floor"])
        self.assertEqual(config.get_threshold("protos", "Fenix", "beta"), 1)


class TestFenixFilingPolicy(unittest.TestCase):
    """D4 as shipped on 2026-09-15 afternoon was HELD (ingested, scored, triaged, filing nothing,
    so a week of verdicts at the rung could be counted -- plans/16 §11.4). Calixte ARMED it the
    same evening on the first tick's first culprit (35e32be2, ``nsTSubstring<T>::Truncate |
    gfxPlatform::ReportTelemetry`` at 85, corroborated), at beta's and release's starting
    policy: ``skip`` and a cap of 2. The hold mechanism stays -- a product hold binds the
    ordinary filer, the spike filer AND a ``file_bug: true`` trigger, whatever the channel -- and
    is pinned here through a patched config."""

    def test_fenix_is_declared_and_armed_and_firefox_is_neither_held_nor_undeclared(self):
        self.assertTrue(config.autofile_product_declared("Fenix"))
        self.assertFalse(config.autofile_product_held("Fenix"))
        # Firefox is the product the top-level block describes (`default_product`): declared
        # without an entry, exactly as nightly is by `default_channel`.
        self.assertTrue(config.autofile_product_declared("Firefox"))
        self.assertFalse(config.autofile_product_held("Firefox"))
        self.assertEqual(config.get_agent()["autofile"]["default_product"], "Firefox")
        # A product nobody decided about is a GAP, not a hold; the two must stay distinct states.
        for product in ("Focus", "Thunderbird", "", None):
            with self.subTest(product=product):
                self.assertFalse(config.autofile_product_declared(product))
                self.assertFalse(config.autofile_product_held(product))
        # The entry carries an EXPLICIT `enabled` key -- the overlay shape that makes the
        # decision legible (a bare `{}` would arm the product at the top-level policy).
        self.assertIs(config.get_agent()["autofile"]["products"]["Fenix"]["enabled"], True)

    def test_the_product_layer_moves_exactly_its_two_keys_and_the_kill_switch_still_wins(self):
        """Under prod's live value: with ``AUTOFILE_BUGS=1`` Firefox nightly files at nightly's
        policy and Fenix nightly at its own -- ``skip``, cap 2 -- and those are the ONLY keys the
        product layer moves. ``AUTOFILE_BUGS=0`` kills both."""
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            nightly = config.get_agent_autofile("nightly", "Firefox")
            fenix = config.get_agent_autofile("nightly", "Fenix")
            self.assertTrue(nightly["enabled"])
            self.assertTrue(fenix["enabled"])
            self.assertEqual({k: v for k, v in fenix.items() if nightly[k] != v},
                             {"comment_on_existing": "skip"})
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "0"}):
            self.assertFalse(config.get_agent_autofile("nightly", "Fenix")["enabled"])
            self.assertFalse(config.get_agent_autofile("nightly", "Firefox")["enabled"])

    def test_a_held_product_beats_the_global_arm_on_every_channel(self):
        """The 2026-09-15 afternoon shape, through a patched config: an explicit
        ``products.Fenix.enabled: false`` survives ``AUTOFILE_BUGS=1`` and ``enabled`` is the
        ONLY key it moves -- a statement about the PRODUCT, held on any channel, paired or not."""
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["products"] = {**autofile["products"], "Fenix": {"enabled": False}}
        agent["autofile"] = autofile
        with mock.patch.object(config, "get_agent", return_value=agent), \
                mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            self.assertTrue(config.autofile_product_held("Fenix"))
            nightly = config.get_agent_autofile("nightly", "Firefox")
            fenix = config.get_agent_autofile("nightly", "Fenix")
            self.assertTrue(nightly["enabled"])
            self.assertFalse(fenix["enabled"])
            self.assertEqual({k: v for k, v in fenix.items() if nightly[k] != v},
                             {"enabled": False})
            for channel in ("beta", "release", "esr153", None):
                self.assertFalse(config.get_agent_autofile(channel, "Fenix")["enabled"], channel)

    def test_no_product_is_byte_identical_to_before(self):
        """``product=None`` merges nothing: what keeps every ``return_value=`` mock and every
        one-argument caller of ``get_agent_autofile`` honest after the signature grew."""
        for channel in ("nightly", "beta", "release", "esr153", None):
            with self.subTest(channel=channel):
                self.assertEqual(config.get_agent_autofile(channel),
                                 config.get_agent_autofile(channel, None))
                self.assertEqual(config.get_agent_autofile(channel),
                                 config.get_agent_autofile(channel, "Firefox"))
        self.assertEqual(config.get_agent_autofile(), config.get_agent_autofile("nightly", "Firefox"))

    def test_a_product_overlay_layers_on_top_of_the_channel_overlay(self):
        """Hypothetical, through a patched ``get_agent``: the day Fenix is armed, its entry may
        tighten a channel's policy (a lower cap) without restating it, and the kill direction of
        the global switch still wins."""
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["products"] = {"Fenix": {"enabled": True, "daily_cap": 1}}
        agent["autofile"] = autofile
        with mock.patch.object(config, "get_agent", return_value=agent):
            self.assertTrue(config.autofile_product_declared("Fenix"))
            self.assertFalse(config.autofile_product_held("Fenix"))
            with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
                pol = config.get_agent_autofile("nightly", "Fenix")
                self.assertTrue(pol["enabled"])
                self.assertEqual(pol["daily_cap"], 1)
                self.assertEqual(pol["comment_on_existing"], "comment")   # nightly's
                # Channel first, product on top: beta's `skip`, the product's cap.
                pol = config.get_agent_autofile("beta", "Fenix")
                self.assertEqual((pol["comment_on_existing"], pol["daily_cap"]), ("skip", 1))
                # Firefox untouched by a Fenix entry.
                self.assertIsNone(config.get_agent_autofile("nightly", "Firefox")["daily_cap"])
            with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "0"}):
                self.assertFalse(config.get_agent_autofile("nightly", "Fenix")["enabled"])


class TestFenixPublishesNoCalibratedProbability(unittest.TestCase):
    """D13: ``agent.calibration.fit_product: "Firefox"``, ``products.Fenix: {}``. The shipped
    table is the fit over 90 rows of ``corpus_ship``, every one Firefox NIGHTLY, and a Fenix run
    lands on channel ``nightly`` == ``fit_channel`` -- so the channel guard that stopped
    release/esr inheriting the fit cannot protect Fenix; the product guard must."""

    SHIPPED = {25: 0.5, 50: 0.5714, 70: 0.7234, 85: 0.7234}

    def test_fenix_gets_nothing_and_firefox_keeps_its_table(self):
        self.assertEqual(config.get_agent_calibration("nightly", "Fenix"), {})
        self.assertEqual(config.get_agent_calibration(None, "Fenix"), {})
        self.assertEqual(config.get_agent_calibration("nightly", "Firefox"), self.SHIPPED)
        # No product == Firefox, byte for byte, on every channel.
        for channel in ("nightly", "beta", "release", "esr153", None):
            with self.subTest(channel=channel):
                self.assertEqual(config.get_agent_calibration(channel),
                                 config.get_agent_calibration(channel, "Firefox"))
                self.assertEqual(config.get_agent_calibration(channel),
                                 config.get_agent_calibration(channel, None))
        self.assertEqual(config.get_agent_calibration(), self.SHIPPED)
        self.assertEqual(config.get_agent_calibration("beta", "Firefox"), {})
        self.assertEqual(config.get_agent()["calibration"]["fit_product"], "Firefox")
        self.assertEqual(config.get_agent()["calibration"]["products"], {"Fenix": {}})

    def test_a_product_named_nowhere_gets_nothing_either(self):
        """The EXPLICIT fallback (``sigage._population``'s shape): only the fit product reads
        the top-level table. Otherwise the next product inherits the fit again -- the class of
        defect the channel fallback was rewritten to close."""
        self.assertEqual(config.get_agent_calibration("nightly", "Focus"), {})

    def test_a_product_entry_may_carry_its_own_fit(self):
        """Hypothetical, through a patched ``get_agent``: the day a Fenix arm of the corpus is
        fit, ``products.Fenix.table`` publishes it and its own ``channels`` still apply."""
        agent = dict(config.get_agent())
        cal = dict(agent["calibration"])
        cal["products"] = {"Fenix": {"table": {"70": 0.6}, "channels": {"beta": {}}}}
        agent["calibration"] = cal
        with mock.patch.object(config, "get_agent", return_value=agent):
            self.assertEqual(config.get_agent_calibration("nightly", "Fenix"), {70: 0.6})
            self.assertEqual(config.get_agent_calibration("beta", "Fenix"), {})
            self.assertEqual(config.get_agent_calibration("nightly", "Firefox"), self.SHIPPED)


class TestTheVenueMap(unittest.TestCase):
    """D14: the android family (Fenix, Focus) additionally excludes BMO ``Firefox`` (desktop).
    Bug 1681745 ``Firefox :: Installer`` was a venue for a Fenix crash under the product-blind
    map, and ``report_bug.resolve_product_component`` could adopt a desktop front-end component
    for one. Desktop's and ``None``'s sets are pinned byte-identical in
    tests/test_other_app_products.py; this is the Fenix side."""

    def test_fenix_excludes_desktop_firefox_on_top_of_the_map(self):
        self.assertEqual(config._PRODUCT_FAMILY, {"Fenix": "android", "Focus": "android"})
        self.assertEqual(config._FAMILY_ONLY_FOREIGN, {"android": ["Firefox"]})
        fenix = config.get_other_app_products("Fenix")
        self.assertEqual(fenix, config.get_other_app_products("Firefox") | {"Firefox"})
        self.assertIn("Firefox", fenix)
        # One family, two Socorro products: 40 of the 96 open `Firefox for Android` signature
        # bugs collide with the Focus population, so Focus reads the same set.
        self.assertEqual(config.get_other_app_products("Focus"), fenix)
        # GeckoView stays SHARED (the measured price: bug 1812544 stays a desktop candidate), and
        # `Firefox for Android` is of course a venue for a Fenix crash.
        for venue in ("Firefox for Android", "GeckoView", "Focus", "Core", "Toolkit"):
            self.assertNotIn(venue, fenix, venue)
        self.assertIsInstance(fenix, frozenset)

    def test_the_prose_names_desktop_firefox_for_a_fenix_crash_only(self):
        fenix = config.describe_other_applications("Fenix")
        self.assertIn("desktop Firefox", fenix)
        self.assertIn("``Firefox``", fenix)
        self.assertIn("Thunderbird", fenix)
        for product in ("Firefox", None):
            self.assertNotIn("desktop Firefox", config.describe_other_applications(product))


class TestTheProductType(unittest.TestCase):
    """The ``PRODUCT_TYPE`` enum, lenient and migrated like ``CHANNEL_TYPE``: ``Fenix`` is the
    first product label added since the initial deploy, and a Postgres enum label is forever."""

    def test_the_type_is_lenient_and_lists_every_configured_product(self):
        self.assertIsInstance(models.PRODUCT_TYPE, models._LenientEnum)
        self.assertEqual(list(models.PRODUCT_TYPE.enums), config.get_products())
        self.assertIn("Fenix", models.PRODUCT_TYPE.enums)       # `api.py` validates against it
        for column in (models.Build.product, models.ChannelDaily.product,
                       models.SignatureDaily.product):
            self.assertIs(column.type, models.PRODUCT_TYPE)

    def test_the_enum_migration_carries_every_product_label(self):
        """The release phase runs ``ALTER TYPE "PRODUCT_TYPE" ADD VALUE IF NOT EXISTS 'Fenix'``
        on the long-lived DB; without it the first Fenix write (``ChannelDaily.upsert`` from
        ``sigtrend.backfill``, which runs FIRST in ``put_crashes``) and every
        ``Build.product == 'Fenix'`` read raise. Proved on Postgres in
        tests/test_enum_migration_pg.py; the labels are ours and still checked before they are
        quoted into DDL."""
        self.assertEqual(models._ENUM_ADDITIONS["PRODUCT_TYPE"], tuple(config.get_products()))
        self.assertEqual(models._ENUM_ADDITIONS["PRODUCT_TYPE"], ("Firefox", "Fenix"))
        for label in models._ENUM_ADDITIONS["PRODUCT_TYPE"]:
            self.assertRegex(label, models._ENUM_LABEL)

    def test_a_stored_product_label_the_config_lacks_still_reads(self):
        """The v166 shape one axis over: should Fenix ever be retired the way esr115/esr140
        were, its ``builds`` / ``chandaily`` / ``sigdaily`` rows must still READ, or every page
        joining them 500s. sqlite here; the Postgres read is in tests/test_enum_migration_pg.py."""
        models.ChannelDaily.__table__.create(bind=db.engine, checkfirst=True)
        db.session.execute(text("DELETE FROM chandaily WHERE product = 'Focus'"))
        db.session.execute(text(
            "INSERT INTO chandaily (product, channel, day, reports, installs) "
            "VALUES ('Focus', 'nightly', '2026-09-01', 7, 3)"))
        db.session.commit()
        try:
            self.assertNotIn("Focus", models.PRODUCT_TYPE.enums)
            rows = {r.product for r in db.session.query(models.ChannelDaily).all()}
            self.assertIn("Focus", rows)
            self.assertIn("Focus", {p for (p,) in db.session.query(models.ChannelDaily.product)})
        finally:
            db.session.execute(text("DELETE FROM chandaily WHERE product = 'Focus'"))
            db.session.commit()


class TestTheSelectionVocabulary(unittest.TestCase):
    """D11: selection-log honesty. ``EMPTY: *`` signatures are declined as ``no_stack`` before
    the spike test (20-40% of Fenix nightly reports: Socorro's Android stackwalker fails at
    scale, plans/16 §5); a kept pair that yields no proto and no uuid is ``no_protos``, not
    ``selected`` -- which used to claim an analysis that never happened (a Stats row with
    installs=0 and no uuid)."""

    def test_the_two_outcomes_are_in_the_vocabulary_and_fit_the_column(self):
        self.assertEqual((utils.NO_STACK, utils.NO_PROTOS), ("no_stack", "no_protos"))
        width = models.Selection.__table__.c.outcome.type.length
        for outcome in (utils.NO_STACK, utils.NO_PROTOS):
            with self.subTest(outcome=outcome):
                self.assertIn(outcome, models.SELECTION_OUTCOMES)
                self.assertLessEqual(len(outcome), width)
                # Neither means "we analysed this pair": `ever_selected` must not record them
                # and the rate path must not treat them as covered.
                self.assertNotIn(outcome, models.SELECTED_OUTCOMES)

    def test_a_declined_row_has_no_picked_build(self):
        bid = utils.get_build_date(20260914002721)
        for outcome in (utils.NO_STACK, utils.NO_PROTOS):
            record = {"signature": "EMPTY: no frame data available", "day": datetime(2026, 9, 14),
                      "count": 40, "index": 3, "baseline": [0, 0, 0], "evaluable": False,
                      "spiked": False, "bids": {bid: 40}, "installs": {bid: 30}, "picked": None,
                      "outcome": outcome}
            row = models.Selection._row(record, "Fenix", "nightly", datetime.now(_UTC))
            self.assertEqual((row["outcome"], row["picked"], row["ever_selected"], row["product"]),
                             (outcome, None, False, "Fenix"))

    def test_signature_class_splits_empty_java_and_native(self):
        """What decides how a selected signature becomes uuids: native pairs from the
        proto-signature facet, Java pairs from ``java_stack_trace`` (D10), empty ones from
        nowhere. A JVM signature is a dotted exception class, ``: at `` and the top frame; a
        native signature never carries ``: at `` after a dotted identifier."""
        self.assertEqual(utils.signature_class("EMPTY: no frame data available"), "empty")
        self.assertEqual(utils.signature_class("EMPTY: no crashing thread identified"), "empty")
        for sgn in ("java.lang.OutOfMemoryError: at java.util.Arrays.copyOf(Arrays.java)",
                    "mozilla.appservices.fxaclient.FxaException$Forbidden: at "
                    "mozilla.appservices.fxaclient.FfiConverterTypeFxaException.read",
                    "kotlin.KotlinNullPointerException: at org.mozilla.fenix.HomeActivity.onCreate"):
            self.assertEqual(utils.signature_class(sgn), "java", sgn)
        for sgn in ("mozilla::places::History::History", "OOM | large",
                    "js::jit::AttachBaselineCacheIRStub", "IPCError-browser | ShutDownKill",
                    "mozilla::dom::Foo::Bar: at", "EMPTY", "", None):
            self.assertEqual(utils.signature_class(sgn), "native", sgn)


class TestTheJavaPrefAndScorer(unittest.TestCase):
    """D7 / D8: ``java.trust_line_numbers`` false and the four package prefixes.

    Fenix nightly APKs are R8-minified and Socorro's ``java_stack_trace`` carries the REMAPPED
    line numbers: at the build revision of crash 3c426d92 (2026-09-11),
    ``Keystore.generateKey(Keystore.kt:269)`` points inside a different method (``fun
    generateKey`` is line 221) and ``FxaAccountManager.kt:3`` is the licence header. The FILE
    and METHOD are right, the LINE is not, so an untrusted frame scores by file/method
    (``Changeset._fuzzy_score``) and never by line proximity."""

    def test_the_shipped_pref_and_packages(self):
        self.assertFalse(config.java_trust_line_numbers())
        self.assertEqual(config.java_packages(), ("org.mozilla.", "mozilla.components.",
                                                  "mozilla.appservices.", "mozilla.telemetry."))
        for fqcn in ("org.mozilla.fenix.HomeActivity", "org.mozilla.gecko.GeckoThread",
                     "mozilla.components.lib.dataprotect.Keystore.generateKey",
                     "mozilla.appservices.fxaclient.FxaAccountManager",
                     "mozilla.telemetry.glean.Glean"):
            self.assertTrue(config.is_java_package(fqcn), fqcn)
        # The old `org.mozilla.` literal rejected every `mozilla.components.*` frame of the
        # example; the platform and libraries are still somebody else's.
        for fqcn in ("java.lang.Thread", "android.os.Handler", "androidx.fragment.app.Fragment",
                     "kotlinx.coroutines.BuildersKt", "com.google.android.gms.X",
                     "mozilla.telemetryx.Y", "", None):
            self.assertFalse(config.is_java_package(fqcn), fqcn)

    def test_the_pref_is_flippable_and_the_defaults_fill_a_partial_block(self):
        """``true`` restores line-proximity scoring the day the per-build R8 mapping is consumed.
        A data pref, not a kill switch, so no env override (the ship-live rule)."""
        g = dict(config._get_global())
        g["java"] = {"trust_line_numbers": True}
        with mock.patch.object(config, "_get_global", return_value=g):
            self.assertTrue(config.java_trust_line_numbers())
            self.assertEqual(config.java_packages(), ("org.mozilla.",))    # `_JAVA_DEFAULTS`
        g["java"] = None
        with mock.patch.object(config, "_get_global", return_value=g):
            self.assertFalse(config.java_trust_line_numbers())

    def test_the_fuzzy_rungs_sit_between_zero_and_the_line_scorers_max(self):
        """``file_match`` 5 is the literal ``sc < 5`` boundary the line-proximity scorer already
        calls "near"; ``method_match`` 8 sits above it and below the 10 a new file or an exact
        line gets, so a changeset that touched the crashing method outranks one that touched the
        file elsewhere, and both rank below one that created the file."""
        self.assertEqual(config.get_file_match_score(), 5)
        self.assertEqual(config.get_method_match_score(), 8)
        self.assertEqual(config.get_max_score(), 10)
        self.assertLess(0, config.get_file_match_score())
        self.assertLess(config.get_file_match_score(), config.get_method_match_score())
        self.assertLess(config.get_method_match_score(), config.get_max_score())

    @staticmethod
    def _chg(cid, touched=(), added=(), deleted=(), isnew=False):
        return SimpleNamespace(id=cid, touched_lines=list(touched), added_lines=list(added),
                               deleted_lines=list(deleted), isnew=isnew)

    def test_fuzzy_score_method_then_file_then_nothing(self):
        span = (221, 260)                                    # `fun generateKey` at the build rev
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1, touched=[230]), span), 8)
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1, added=[221]), span), 8)
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1, deleted=[260]), span), 8)
        # Touched the file, outside the method: file-level. Line 269 -- the REPORTED line -- is
        # outside the span, which is the whole point: it must not be a near-miss of anything.
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1, touched=[269]), span), 5)
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1, touched=[3]), span), 5)
        # No span located (the method could not be found in the source): file-level at most.
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1, touched=[230]), None), 5)
        # A changeset that recorded no lines says nothing.
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1), span), 0)
        self.assertEqual(models.Changeset._fuzzy_score(self._chg(1), None), 0)

    def test_get_scores_uses_the_fuzzy_rungs_only_when_the_line_is_untrusted(self):
        rows = [self._chg(11, isnew=True), self._chg(12, touched=[230]), self._chg(13, touched=[3]),
                self._chg(14)]
        with mock.patch.object(models.db.session, "query", return_value=_FakeQuery(rows)):
            scores = models.Changeset.get_scores("a/b/Keystore.kt", 269, ["n1"], 7,
                                                 channel="nightly", line_trusted=False,
                                                 method_lines=(221, 260))
        self.assertEqual(scores, [(11, 7, 10), (12, 7, 8), (13, 7, 5), (14, 7, 0)])
        # The trusted path is untouched: the fuzzy scorer is never consulted, an exact line
        # scores the max, and a far line scores by distance (here 3 -> 269 is 0).
        rows = [self._chg(21, touched=[269]), self._chg(22, touched=[3]), self._chg(23, isnew=True)]
        with mock.patch.object(models.db.session, "query", return_value=_FakeQuery(rows)), \
                mock.patch.object(models.Changeset, "_fuzzy_score",
                                  side_effect=AssertionError("a trusted line must not be fuzzy")):
            scores = models.Changeset.get_scores("a/b/Keystore.kt", 269, ["n1"], 7,
                                                 channel="nightly")
        self.assertEqual(scores, [(21, 7, 10), (22, 7, 0), (23, 7, 10)])


class TestThePathResolver(unittest.TestCase):
    """D9: ``models.File`` rows for ``mobile/android/**.{kt,java}``, matched by PACKAGE-PATH
    SUFFIX. Measured on the live mobile/android tree: 329 ambiguous basenames, 11 ambiguous
    package-path+file suffixes, every one a test / androidTest / samples copy of a ``src/main``
    file -- and ``.first()`` used to pick one in undefined order."""

    MAIN = ("mobile/android/android-components/components/lib/dataprotect/src/main/java/"
            "mozilla/components/lib/dataprotect/Keystore.kt")
    TEST = ("mobile/android/android-components/components/lib/dataprotect/src/test/java/"
            "mozilla/components/lib/dataprotect/Keystore.kt")
    SAMPLE = ("mobile/android/android-components/samples/dataprotect/src/main/java/"
              "mozilla/components/lib/dataprotect/Keystore.kt")
    DECOY = "mobile/android/fenix/app/src/main/java/org/mozilla/fenix/home/HomeXFragment.kt"
    ROWS = (MAIN, TEST, SAMPLE, DECOY)

    def setUp(self):
        models.File.__table__.create(bind=db.engine, checkfirst=True)
        self._clean()
        for name in self.ROWS:
            db.session.add(models.File(name))
        db.session.commit()

    def tearDown(self):
        self._clean()

    def _clean(self):
        db.session.rollback()
        db.session.query(models.File).filter(models.File.name.in_(self.ROWS)).delete(
            synchronize_session=False)
        db.session.commit()

    def test_the_shipping_source_wins_over_its_test_and_sample_copies(self):
        for name in ("mozilla/components/lib/dataprotect/Keystore.kt",
                     "lib/dataprotect/Keystore.kt", "dataprotect/Keystore.kt", "Keystore.kt"):
            with self.subTest(name=name):
                self.assertEqual(models.File.get_full_path(name), self.MAIN)
        # The sample copy is itself under `src/main`, so the tie-break after the shipping-dir
        # test is the samples/test-dir test, then the shortest path -- and it is DETERMINISTIC.
        self.assertEqual(models.File.get_full_path("Keystore.kt"), self.MAIN)

    def test_a_miss_returns_the_input_and_the_suffix_is_a_whole_component(self):
        for name in ("org/mozilla/fenix/Nope.kt", "Nope.kt", "", None):
            with self.subTest(name=name):
                self.assertEqual(models.File.get_full_path(name), name)
        # `%/<name>`: a suffix inside a path component is not a match.
        self.assertEqual(models.File.get_full_path("store.kt"), "store.kt")

    def test_like_wildcards_in_a_kotlin_path_are_escaped(self):
        """``_`` is a LIKE wildcard: unescaped, ``Home_Fragment.kt`` would resolve to the decoy
        ``HomeXFragment.kt`` row. It has to be a miss."""
        self.assertEqual(models.File.get_full_path("org/mozilla/fenix/home/Home_Fragment.kt"),
                         "org/mozilla/fenix/home/Home_Fragment.kt")
        self.assertEqual(models.File.get_full_path("home/Home%.kt"), "home/Home%.kt")


class TestTheProductClauses(unittest.TestCase):
    """D12 and the daily cap: per (signature, protohash, channel, PRODUCT). 21.8% of Fenix
    nightly's native signatures also occur on Firefox nightly with at least one byte-identical
    protohash (plans/16 §6.2), so without the clause whichever product was analysed first would
    close the cluster for the other -- and the dangerous direction is the new product silently
    reducing DESKTOP coverage. Decided (plans/16 §11.1): independent clusters, one run per
    product on a shared cluster. Read as SQL, like tests/test_beta_sweep_and_dedup.py's
    Postgres half reads the real query."""

    @staticmethod
    def _sql(query):
        """Compiled for Postgres, whatever `DATABASE_URL` the run has, so the bind names are
        stable (`%(product_1)s`, not sqlite's `?`)."""
        return str(query.statement.compile(dialect=postgresql.dialect()))

    def test_the_cluster_query_carries_the_product_when_given(self):
        with_product = self._sql(models._cluster_dossiers(1, "h", "nightly", "Fenix"))
        self.assertIn("builds_1.product = %(product_1)s", with_product)
        self.assertIn("builds_1.channel = %(channel_1)s", with_product)
        # `None` (legacy callers) keeps the channel-only cluster.
        without = self._sql(models._cluster_dossiers(1, "h", "nightly"))
        self.assertNotIn("product", without)
        self.assertIn("builds_1.channel = %(channel_1)s", without)
        # ...and correlates against the candidate's own build row, the way `untriaged` uses it.
        correlated = self._sql(models._cluster_dossiers(
            models.UUID.signatureid, models.UUID.protohash, models.Build.channel,
            models.Build.product))
        self.assertIn("builds_1.product = builds.product", correlated)

    @staticmethod
    def _outer_only(fake):
        """Intercept the OUTER query and let the cluster subquery (`Dossier.id`) build for real,
        so its correlated SQL lands in the recorded filter."""
        real = models.db.session.query

        def query(*entities, **kw):
            if entities and str(entities[0]) == "Dossier.id":
                return real(*entities, **kw)
            return fake
        return mock.patch.object(models.db.session, "query", side_effect=query)

    def test_untriaged_correlates_the_product_and_filters_on_it(self):
        fake = _FakeQuery()
        with self._outer_only(fake):
            self.assertEqual(models.UUID.untriaged(0, 21600, 1209600, 3, products=["Fenix"]), [])
        self.assertIn("builds_1.product = builds.product", fake.sql)   # the cluster, per product
        self.assertIn("builds.product IN", fake.sql)                   # `config.get_agent_products`
        # `None` is "caller did not ask": no product filter, the cluster still per product.
        fake = _FakeQuery()
        with self._outer_only(fake):
            models.UUID.untriaged(0, 21600, 1209600, 3, channels=["nightly"])
        self.assertNotIn("builds.product IN", fake.sql)
        self.assertIn("builds.channel IN", fake.sql)
        self.assertIn("builds_1.product = builds.product", fake.sql)
        # `[]` is "no product" and matches nothing, like `AGENT_CHANNELS=""` for channels.
        fake = _FakeQuery()
        with self._outer_only(fake):
            models.UUID.untriaged(0, 21600, 1209600, 3, products=[])
        self.assertIn("builds.product IN", fake.sql)

    def test_the_daily_cap_is_per_product_as_well_as_per_channel(self):
        """Fenix nightly and Firefox nightly share the channel label, so without the product
        the two would spend one cap of 10 in both directions the day Fenix files."""
        when = datetime(2026, 9, 15, tzinfo=_UTC)
        fake = _FakeQuery()
        with mock.patch.object(models.db.session, "query", return_value=fake):
            self.assertEqual(models.Dossier.filed_bugs_since(when, channel="nightly",
                                                             product="Fenix"), 0)
        self.assertIn("builds.channel = :channel_1", fake.sql)
        self.assertIn("builds.product = :product_1", fake.sql)
        self.assertEqual(fake.joins, 2)
        fake = _FakeQuery()
        with mock.patch.object(models.db.session, "query", return_value=fake):
            models.Dossier.filed_bugs_since(when, channel="nightly")
        self.assertNotIn("product", fake.sql)
        fake = _FakeQuery()
        with mock.patch.object(models.db.session, "query", return_value=fake):
            models.Dossier.filed_bugs_since(when)
        self.assertEqual(fake.joins, 0)                                 # the legacy shape

    def test_the_tasks_list_says_which_product(self):
        """Fenix nightly and Firefox nightly are both 'N' in the version column."""
        compiled = models.Dossier._list_tasks_query(5).statement.compile(
            dialect=postgresql.dialect())
        self.assertIn("builds.product AS product", str(compiled))
        self.assertIn("builds.channel AS channel", str(compiled))


class TestUpdatePairsFenixWithNightly(unittest.TestCase):
    """D5 / D6 and the ingestion path: ``update_all`` pairs Fenix with nightly only,
    ``update_builds`` dispatches through ``buildsource`` (the TaskCluster index for Fenix, the
    same ``buildhub`` object for everything else) and ``put_report`` marks a report with no
    stack analysed. The pairing/guard cases on ``update()`` itself are in
    tests/test_update.py beside their channel analogs."""

    def test_the_tick_pairs_fenix_with_nightly_only(self):
        pairs = []
        with mock.patch.object(update, "update_in_queue",
                               side_effect=lambda channel, product: pairs.append((product, channel))), \
                mock.patch.dict(os.environ, {"INGEST_CHANNELS": "nightly beta release esr153"}):
            os.environ.pop("INGEST_PRODUCTS", None)
            update.update_all()
        self.assertEqual([c for p, c in pairs if p == "Firefox"],
                         ["nightly", "beta", "release", "esr153"])
        self.assertEqual([c for p, c in pairs if p == "Fenix"], ["nightly"])
        self.assertEqual(len(pairs), 5)

    def test_buildsource_dispatch(self):
        """``for_product("Firefox") is buildhub``: the desktop path is the identical function
        object, not a wrapper -- what keeps it byte-for-byte unchanged. Buildhub has no Fenix at
        all (``source.product`` over its 1.79M documents: firefox, thunderbird, devedition,
        fennec, flowstate), and querying it AS firefox is wrong for the build SET (46
        fenix-nightly builds against 47 firefox-nightly buildids over 23 days; one changeset
        window collapsed from 173 to 2)."""
        self.assertIs(buildsource.for_product("Firefox"), buildsource.buildhub)
        self.assertIs(buildsource.for_product("Thunderbird"), buildsource.buildhub)
        self.assertIs(buildsource.for_product(None), buildsource.buildhub)
        self.assertEqual(buildsource.TC_PRODUCTS, frozenset({"Fenix"}))
        self.assertIn("Fenix", buildsource.TC_PRODUCTS)

    def _tc_source(self, data):
        source = mock.Mock(name="tcindex")
        source.get.return_value = data
        return source

    def test_a_tc_product_starts_a_day_before_the_newest_stored_build(self):
        """D6: the ``builds`` table IS the cache. The index is probed one day namespace and one
        leaf at a time (no server-side range query), so a warm table pays ~1 day of probes per
        tick instead of the 30-day lookback (~30 POST + ~150 GET); a leaf attaches ~18 minutes
        after its push, so the day before the newest build is re-probed until it resolves.
        The clamp applies to the DERIVED date (the tick's ``LastDate - lookback``) only."""
        maxdate = datetime(2026, 9, 15, 12, 0, 0, tzinfo=_UTC)
        newest = datetime(2026, 9, 14, 0, 27, 21, tzinfo=_UTC)
        later = datetime(2026, 9, 15, 0, 26, 12, tzinfo=_UTC)
        data = {"Fenix": {"nightly": {later: {"revision": "1ace7e56a446", "version": "160.0a1"}}}}
        source = self._tc_source(data)
        with mock.patch.object(update.buildsource, "for_product", return_value=source), \
                mock.patch.object(update.models.LastDate, "get", return_value=(None, maxdate)), \
                mock.patch.object(update.models.Build, "get_max_buildid",
                                  return_value=newest) as newest_q, \
                mock.patch.object(update.models.Build, "put_data") as put, \
                mock.patch.object(update.java, "refresh_file_index", create=True) as refresh:
            update.update_builds(None, "nightly", "Fenix")
        source.get.assert_called_once_with(newest - relativedelta(days=1), "nightly", prods="Fenix")
        put.assert_called_once_with(data)
        # The JVM file index refresh runs every tick (idempotent per build inside `java`).
        refresh.assert_called_once_with("nightly", "Fenix")
        self.assertEqual(newest_q.call_args_list, [mock.call("nightly", "Fenix")])

    def test_an_explicit_date_is_a_backfill_and_is_honoured_untouched(self):
        """A day the index lost to a 5xx during the cold sweep is only recoverable by
        ``update.update("2026-09-03", "nightly", "Fenix")``; clamping that request to
        ``newest - 1 day`` would walk yesterday again and report "finished". Same rule as
        ``put_filelog``'s explicit ``start_date``."""
        explicit = datetime(2026, 9, 3, tzinfo=_UTC)
        source = self._tc_source({})
        with mock.patch.object(update.buildsource, "for_product", return_value=source), \
                mock.patch.object(update.models.Build, "get_max_buildid") as newest_q, \
                mock.patch.object(update.models.Build, "put_data") as put, \
                mock.patch.object(update.java, "refresh_file_index", create=True):
            update.update_builds(explicit, "nightly", "Fenix")
        source.get.assert_called_once_with(explicit, "nightly", prods="Fenix")
        newest_q.assert_not_called()
        put.assert_not_called()

    def test_a_cold_table_pays_the_whole_lookback_and_the_index_refresh_runs_every_tick(self):
        maxdate = datetime(2026, 9, 15, 12, 0, 0, tzinfo=_UTC)
        lookback = maxdate - relativedelta(days=config.get_buildhub_lookback_ndays())
        newest = datetime(2026, 9, 14, 0, 27, 21, tzinfo=_UTC)
        data = {"Fenix": {"nightly": {newest: {"revision": "1ace7e56a446", "version": "160.0a1"}}}}
        # Cold: nothing stored, so the lookback is the start.
        source = self._tc_source(data)
        with mock.patch.object(update.buildsource, "for_product", return_value=source), \
                mock.patch.object(update.models.LastDate, "get", return_value=(None, maxdate)), \
                mock.patch.object(update.models.Build, "get_max_buildid", return_value=None), \
                mock.patch.object(update.models.Build, "put_data"), \
                mock.patch.object(update.java, "refresh_file_index", create=True) as refresh:
            update.update_builds(None, "nightly", "Fenix")
        source.get.assert_called_once_with(lookback, "nightly", prods="Fenix")
        refresh.assert_called_once_with("nightly", "Fenix")
        # No build resolved (a 404 leaf = not a CI APK = no row): nothing written, but the
        # refresh still runs -- it is what retries a GitHub 403 on the next tick.
        source = self._tc_source({})
        with mock.patch.object(update.buildsource, "for_product", return_value=source), \
                mock.patch.object(update.models.LastDate, "get", return_value=(None, maxdate)), \
                mock.patch.object(update.models.Build, "get_max_buildid", return_value=newest), \
                mock.patch.object(update.models.Build, "put_data") as put, \
                mock.patch.object(update.java, "refresh_file_index", create=True) as refresh:
            update.update_builds(None, "nightly", "Fenix")
        put.assert_not_called()
        refresh.assert_called_once_with("nightly", "Fenix")
        # A source that breaks its never-raise promise costs the tick nothing but a log line.
        source = self._tc_source({})
        source.get.side_effect = RuntimeError("index down")
        with mock.patch.object(update.buildsource, "for_product", return_value=source), \
                mock.patch.object(update.models.LastDate, "get", return_value=(None, maxdate)), \
                mock.patch.object(update.models.Build, "get_max_buildid", return_value=newest), \
                mock.patch.object(update.models.Build, "put_data") as put, \
                mock.patch.object(update.java, "refresh_file_index", create=True):
            update.update_builds(None, "nightly", "Fenix")
        put.assert_not_called()

    def test_an_index_refresh_failure_costs_a_frame_its_path_never_the_tick(self):
        lookback = datetime(2026, 8, 16, tzinfo=_UTC)
        newest = datetime(2026, 9, 14, 0, 27, 21, tzinfo=_UTC)
        data = {"Fenix": {"nightly": {newest: {"revision": "1ace7e56a446", "version": "160.0a1"}}}}
        source = self._tc_source(data)
        with mock.patch.object(update.buildsource, "for_product", return_value=source), \
                mock.patch.object(update.models.Build, "get_max_buildid",
                                  side_effect=[None, newest]), \
                mock.patch.object(update.models.Build, "put_data") as put, \
                mock.patch.object(update.java, "refresh_file_index", create=True,
                                  side_effect=RuntimeError("GitHub 403")), \
                self.assertLogs(level="WARNING") as logs:
            update.update_builds(lookback, "nightly", "Fenix")
        put.assert_called_once_with(data)
        self.assertTrue(any("java file index not refreshed" in line for line in logs.output))

    def test_the_firefox_path_is_buildhub_and_reads_no_cache(self):
        """Desktop unchanged: one Buildhub POST from the lookback, no ``get_max_buildid`` read,
        no JVM index refresh."""
        lookback = datetime(2026, 8, 16, tzinfo=_UTC)
        bid = datetime(2026, 9, 14, 9, 17, tzinfo=_UTC)
        data = {"Firefox": {"nightly": {bid: {"revision": "1ace7e56a446", "version": "160.0a1"}}}}
        with mock.patch.object(update.buildsource.buildhub, "get", return_value=data) as get, \
                mock.patch.object(update.models.Build, "get_max_buildid",
                                  side_effect=AssertionError("Buildhub has a range query")), \
                mock.patch.object(update.models.Build, "put_data") as put, \
                mock.patch.object(update.java, "refresh_file_index", create=True,
                                  side_effect=AssertionError("no JVM index for desktop")):
            update.update_builds(lookback, "nightly", "Firefox")
        get.assert_called_once_with(lookback, "nightly", prods="Firefox")
        put.assert_called_once_with(data)

    def test_a_report_with_no_stack_is_marked_analysed(self):
        """Neither a json_dump nor a readable java_stack_trace: MARK IT, or the serial scoring
        chain livelocks -- ``UUID.to_analyze`` re-selects analyzed=False rows in id order and
        ``analyze_one_report`` re-enqueues itself, so an unmarked report is fetched from Socorro
        again on every spin, forever. Unreachable for desktop (a proto-selected crash has a
        json_dump); routine on Fenix, where the Android stackwalker fails on 38-72% of native
        reports."""
        buildid = utils.get_build_date("20260914002721")
        with mock.patch.object(update.inspector, "get_crash", return_value=None) as crash, \
                mock.patch.object(update.models.UUID, "set_analyzed") as mark, \
                mock.patch.object(update.models.Changeset, "to_analyze",
                                  side_effect=AssertionError("nothing to score")), \
                mock.patch("crashclouseau.agent.orchestrator.enqueue_agent",
                           side_effect=AssertionError("nothing to enqueue")):
            self.assertIsNone(update.put_report("u-empty", buildid, "nightly", "Fenix", "abc"))
        mark.assert_called_once_with("u-empty", True)
        # The nightly candidate window, as for desktop: `ndays` before the build.
        self.assertEqual(crash.call_args.args[3], buildid - relativedelta(days=config.get_ndays()))

    def test_a_scored_report_enqueues_the_agent_with_its_product(self):
        """``enqueue_agent(uuid, channel, product=)``: the gate on ``config.get_agent_products``
        (D3) needs the product, and ``UUID.get_channel`` alone cannot supply it."""
        buildid = utils.get_build_date("20260914002721")
        frames = {"hash": "deadbeef", "frames": []}
        with mock.patch.object(update.inspector, "get_crash", return_value={"nonjava": frames}), \
                mock.patch.object(update.models.Changeset, "to_analyze", return_value=[]), \
                mock.patch.object(update.models.UUID, "is_stackhash_existing", return_value=False), \
                mock.patch.object(update.models.CrashStack, "put_frames"), \
                mock.patch.object(update.models.UUID, "add_stack_hash"), \
                mock.patch.object(update.models.UUID, "set_analyzed") as mark, \
                mock.patch("crashclouseau.agent.orchestrator.enqueue_agent") as enqueue:
            self.assertTrue(update.put_report("u-scored", buildid, "nightly", "Fenix", "abc"))
        mark.assert_called_once_with("u-scored", False)
        enqueue.assert_called_once_with("u-scored", "nightly", product="Fenix")


if __name__ == "__main__":
    unittest.main()
