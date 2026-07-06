# DATABASE_URL=sqlite:// REDIS_URL=... python -m unittest tests.test_update
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import contextlib  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import update  # noqa: E402


class TestUpdateAllChannels(unittest.TestCase):
    """update_all ingests $INGEST_CHANNELS when set, else all configured channels —
    lets a canary ingest nightly-only without touching the shared (enum-defining) config."""

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

    def test_default_is_all_channels(self):
        self.assertEqual(self._channels_used(None), ["nightly", "beta", "release"])

    def test_empty_env_falls_back_to_all(self):
        self.assertEqual(self._channels_used(""), ["nightly", "beta", "release"])

    def test_ingest_channels_nightly_only(self):
        self.assertEqual(self._channels_used("nightly"), ["nightly"])

    def test_ingest_channels_subset(self):
        self.assertEqual(self._channels_used("nightly beta"), ["nightly", "beta"])


if __name__ == "__main__":
    unittest.main()
