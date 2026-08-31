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


if __name__ == "__main__":
    unittest.main()
