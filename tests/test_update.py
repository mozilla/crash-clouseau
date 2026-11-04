# DATABASE_URL=sqlite:// REDIS_URL=... python -m unittest tests.test_update
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import contextlib  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from crashclouseau import db, models, update  # noqa: E402


class TestUpdateAllChannels(unittest.TestCase):
    """update_all ingests exactly $INGEST_CHANNELS, and NOTHING when it is unset or empty.

    The old default was "else all configured channels", which meant an unset variable ingested
    release. It fired: see `test_shipped_channels.test_ingest_channels_must_always_be_set_
    explicitly` for the 7,267 rows production is still carrying."""

    def _channels_used(self, env):
        calls = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                update, "update_in_queue",
                side_effect=lambda channel, product: calls.append(channel)))
            stack.enter_context(mock.patch.object(
                update.config, "get_products", return_value=["Firefox"]))
            stack.enter_context(mock.patch.object(
                update.config, "get_channels",
                return_value=["nightly", "beta", "release"]))
            stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
            if env is None:
                os.environ.pop("INGEST_CHANNELS", None)
            else:
                os.environ["INGEST_CHANNELS"] = env
            update.update_all()
        return calls

    def test_unset_env_ingests_nothing(self):
        """NOT "all configured channels". `config.get_channels()` also defines the CHANNEL_TYPE
        enum, so it contains every channel ever contemplated -- it is the wrong list to default
        an action to."""
        self.assertEqual(self._channels_used(None), [])

    def test_empty_env_ingests_nothing(self):
        self.assertEqual(self._channels_used(""), [])

    def test_whitespace_only_env_ingests_nothing(self):
        self.assertEqual(self._channels_used("   "), [])

    def test_ingest_channels_nightly_only(self):
        self.assertEqual(self._channels_used("nightly"), ["nightly"])

    def test_ingest_channels_subset(self):
        self.assertEqual(self._channels_used("nightly beta"), ["nightly", "beta"])


class TestUpdateRefusesAChannelTheDeploymentDoesNotIngest(unittest.TestCase):
    """`update_all` reads INGEST_CHANNELS, but the job it enqueues carries the channel as an
    argument -- so a queued or RQ-re-run job for a channel just dropped from the variable would
    ingest it once more (esr115/esr140, 2026-09-07), and a label `config.channels` no longer
    lists would land in the enum column as residue. `update()` is the one entry point."""

    def _run(self, channel, env):
        calls = []
        with contextlib.ExitStack() as stack:
            for name in ("put_filelog", "update_builds", "put_crashes", "analyze_reports"):
                stack.enter_context(mock.patch.object(
                    update, name, side_effect=lambda *a, _n=name, **k: calls.append(_n)))
            stack.enter_context(mock.patch.dict(os.environ, {"INGEST_CHANNELS": env}))
            update.update(None, channel, "Firefox")
        return calls

    def test_a_channel_outside_the_config_does_nothing(self):
        self.assertNotIn("esr115", update.config.get_channels())
        self.assertEqual(self._run("esr115", "nightly beta esr115"), [])

    def test_a_channel_outside_ingest_channels_does_nothing(self):
        self.assertIn("release", update.config.get_channels())
        self.assertEqual(self._run("release", "nightly beta"), [])

    def test_an_ingested_channel_runs_every_step(self):
        self.assertEqual(self._run("nightly", "nightly beta"),
                         ["put_filelog", "update_builds", "put_crashes", "analyze_reports"])


class TestUpdateAllPairsEachProductWithItsChannels(unittest.TestCase):
    """Use ingestion products and filter channels through ``product_channels``."""

    def _pairs_used(self, channels, products=None):
        pairs = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                update, "update_in_queue",
                side_effect=lambda channel, product: pairs.append((product, channel))))
            stack.enter_context(mock.patch.dict(os.environ, {"INGEST_CHANNELS": channels}))
            if products is None:
                os.environ.pop("INGEST_PRODUCTS", None)
            else:
                os.environ["INGEST_PRODUCTS"] = products
            update.update_all()
        return pairs

    def test_fenix_is_paired_with_every_channel_but_esr(self):
        pairs = self._pairs_used("nightly beta release esr153")
        self.assertEqual(pairs, [("Firefox", "nightly"), ("Firefox", "beta"),
                                 ("Firefox", "release"), ("Firefox", "esr153"),
                                 ("Fenix", "nightly"), ("Fenix", "beta"), ("Fenix", "release")])

    def test_an_esr_only_deployment_ingests_no_fenix_at_all(self):
        self.assertEqual(self._pairs_used("esr153"), [("Firefox", "esr153")])

    def test_ingest_products_restricts_and_an_empty_value_ingests_nothing(self):
        self.assertEqual(self._pairs_used("nightly beta", products="Firefox"),
                         [("Firefox", "nightly"), ("Firefox", "beta")])
        self.assertEqual(self._pairs_used("nightly beta", products="Fenix"),
                         [("Fenix", "nightly"), ("Fenix", "beta")])
        # Set-but-empty is a real kill switch (unset falls back to the config's list).
        self.assertEqual(self._pairs_used("nightly beta", products=""), [])
        # A product the enum does not know cannot be ingested, whatever the variable says.
        self.assertEqual(self._pairs_used("nightly", products="Focus"), [])

    def test_the_unset_default_is_the_configs_list_not_the_enum(self):
        # The lever exists in the config file: both products, and Fenix's pairing.
        self.assertEqual(update.config.get_ingest_products(), ["Firefox", "Fenix"])
        self.assertEqual(update.config.get_product_channels("Fenix"),
                         ["nightly", "beta", "release"])


class TestUpdateRefusesAProductOrPairingTheDeploymentDoesNotIngest(unittest.TestCase):
    """Queued jobs must respect the current ingestion products and channels."""

    def _run(self, channel, product, channels="nightly beta", products=None):
        calls = []
        with contextlib.ExitStack() as stack:
            for name in ("put_filelog", "update_builds", "put_crashes", "analyze_reports"):
                stack.enter_context(mock.patch.object(
                    update, name, side_effect=lambda *a, _n=name, **k: calls.append(_n)))
            stack.enter_context(mock.patch.dict(os.environ, {"INGEST_CHANNELS": channels}))
            if products is None:
                os.environ.pop("INGEST_PRODUCTS", None)
            else:
                os.environ["INGEST_PRODUCTS"] = products
            update.update(None, channel, product)
        return calls

    def test_fenix_nightly_runs_every_step(self):
        self.assertEqual(self._run("nightly", "Fenix"),
                         ["put_filelog", "update_builds", "put_crashes", "analyze_reports"])

    def test_fenix_on_an_unpaired_channel_does_nothing(self):
        self.assertEqual(self._run("esr153", "Fenix", channels="nightly beta esr153"), [])

    def test_fenix_beta_and_release_run_every_step(self):
        for channel in ("beta", "release"):
            with self.subTest(channel=channel):
                self.assertEqual(self._run(channel, "Fenix", channels="nightly beta release"),
                                 ["put_filelog", "update_builds", "put_crashes",
                                  "analyze_reports"])

    def test_a_product_outside_ingest_products_does_nothing(self):
        self.assertEqual(self._run("nightly", "Fenix", products="Firefox"), [])
        self.assertEqual(self._run("nightly", "Firefox", products="Fenix"), [])

    def test_a_product_outside_the_config_does_nothing(self):
        self.assertNotIn("Focus", update.config.get_products())
        self.assertEqual(self._run("nightly", "Focus"), [])

    def test_firefox_is_unchanged(self):
        self.assertEqual(self._run("nightly", "Firefox"),
                         ["put_filelog", "update_builds", "put_crashes", "analyze_reports"])
        self.assertEqual(self._run("beta", "Firefox"),
                         ["put_filelog", "update_builds", "put_crashes", "analyze_reports"])


_SOURCE = {"original": "git:github.com/mozilla-firefox/firefox:js/src/gc/Marking.cpp:ff3b300c"}
_NO_SOURCE = {"original": None}


class TestPutReportStoresAndEnqueues(unittest.TestCase):
    BUILD = datetime(2026, 10, 27, 21, 25, 43, tzinfo=timezone.utc)

    def setUp(self):
        for target, names in (
            (update.inspector, ("get_crash",)),
            (models.Changeset, ("to_analyze",)),
            (models.UUID, ("is_stackhash_existing", "add_stack_hash", "set_analyzed")),
            (models.CrashStack, ("put_frames",)),
        ):
            for name in names:
                setattr(self, name, self.enterContext(mock.patch.object(target, name)))
        self.enqueue = self.enterContext(mock.patch("crashclouseau.agent.orchestrator.enqueue_agent"))
        self.to_analyze.return_value = []
        self.is_stackhash_existing.return_value = False

    def _put(self, res):
        self.get_crash.return_value = res
        return update.put_report("u-1", self.BUILD, "nightly", "Firefox", "abc")

    def test_a_new_source_stack_is_stored_and_enqueued(self):
        self.assertTrue(self._put({"nonjava": {"hash": "h1", "frames": [_SOURCE]}}))
        self.is_stackhash_existing.assert_called_once_with("u-1", "h1", False)
        self.put_frames.assert_called_once()
        self.add_stack_hash.assert_called_once_with("u-1", "h1", "")
        self.set_analyzed.assert_called_once_with("u-1", False)
        self.enqueue.assert_called_once_with("u-1", "nightly", product="Firefox")

    def test_a_stack_without_a_source_frame_is_stored_but_not_enqueued(self):
        for frames in ([], [_NO_SOURCE, {}], [{"original": ""}]):
            with self.subTest(frames=frames):
                self.put_frames.reset_mock()
                self.assertTrue(self._put({"nonjava": {"hash": "h1", "frames": frames}}))
                self.put_frames.assert_called_once()
                self.enqueue.assert_not_called()

    def test_a_java_stack_is_a_source_stack(self):
        res = {"nonjava": {"hash": "h1", "frames": [_NO_SOURCE]},
               "java": {"hash": "j1", "frames": [_NO_SOURCE]}}
        self.assertTrue(self._put(res))
        self.is_stackhash_existing.assert_has_calls([
            mock.call("u-1", "h1", False), mock.call("u-1", "j1", True)])
        self.add_stack_hash.assert_called_once_with("u-1", "h1", "j1")
        self.enqueue.assert_called_once()

    def test_a_duplicate_stack_is_neither_stored_nor_enqueued(self):
        self.is_stackhash_existing.return_value = True
        self.assertFalse(self._put({"nonjava": {"hash": "h1", "frames": [_SOURCE]}}))
        self.put_frames.assert_not_called()
        self.set_analyzed.assert_called_once_with("u-1", True)
        self.enqueue.assert_not_called()


class TestTheStackCheckIsPerSignature(unittest.TestCase):
    """The deduplication query only reads UUID rows."""

    def setUp(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        models.UUID.__table__.create(engine)
        session = self.enterContext(Session(engine))
        self.enterContext(mock.patch.object(db, "session", session))

    def _uuid(self, name, sig=1, stackhash=None, buildid=1, java=False):
        u = models.UUID(name, sig, "proto-" + name, buildid)
        if java:
            u.jstackhash = stackhash
        else:
            u.stackhash = stackhash
        db.session.add(u)
        db.session.commit()

    def _exists(self, name, java=False):
        return models.UUID.is_stackhash_existing(name, "db30bb68", java)

    def test_another_signatures_stack_does_not_count(self):
        self._uuid("other", sig=2, stackhash="db30bb68")
        self._uuid("current")
        self.assertFalse(self._exists("current"))

    def test_the_same_signatures_stack_counts(self):
        self._uuid("other", stackhash="db30bb68")
        self._uuid("current")
        self.assertTrue(self._exists("current"))

    def test_a_report_does_not_match_itself(self):
        self._uuid("current", stackhash="db30bb68")
        self.assertFalse(self._exists("current"))

    def test_another_builds_stack_does_not_count(self):
        self._uuid("other", stackhash="db30bb68", buildid=2)
        self._uuid("current")
        self.assertFalse(self._exists("current"))

    def test_java_and_native_hashes_are_checked_separately(self):
        self._uuid("other", stackhash="db30bb68", java=True)
        self._uuid("current")
        self.assertTrue(self._exists("current", java=True))
        self.assertFalse(self._exists("current"))

    def test_null_signatures_still_match(self):
        self._uuid("other", sig=None, stackhash="db30bb68")
        self._uuid("current", sig=None)
        self.assertTrue(self._exists("current"))


if __name__ == "__main__":
    unittest.main()
