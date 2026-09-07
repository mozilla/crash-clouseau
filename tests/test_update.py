# DATABASE_URL=sqlite:// REDIS_URL=... python -m unittest tests.test_update
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import contextlib  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import update  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
