# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# crashclouseau.net: every direct HTTP call carries our allowlisted User-Agent AND a
# timeout. An unbounded call inside an RQ worker costs a whole triage run.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_net
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import net  # noqa: E402


class TestDefaultTimeout(unittest.TestCase):
    """No verb may issue an unbounded request: `requests` with timeout=None blocks forever,
    which hangs a triage run until RQ kills it and the reaper pays for it all over again."""

    def _call(self, verb, **kwargs):
        with mock.patch("requests." + verb) as r:
            getattr(net, verb)("https://hg.mozilla.org/x", **kwargs)
        return r.call_args

    def test_every_verb_is_bounded(self):
        for verb in ("get", "post", "put"):
            with self.subTest(verb=verb):
                self.assertEqual(self._call(verb).kwargs["timeout"], net.DEFAULT_TIMEOUT)

    def test_connect_and_read_halves(self):
        # (connect, read); the read half is a gap-between-bytes limit, not a total cap, so a
        # big-but-progressing response is not cut off.
        self.assertEqual(len(net.DEFAULT_TIMEOUT), 2)
        connect, read = net.DEFAULT_TIMEOUT
        self.assertGreater(connect, 0)
        self.assertGreater(read, 0)
        # must be well under the RQ job timeout, or a hang still costs the run
        from crashclouseau import config
        self.assertLess(connect + read, config.get_agent_job_timeout())

    def test_explicit_timeout_wins(self):
        # bugzilla_apply passes timeout=60 on its writes; that must not be overridden.
        self.assertEqual(self._call("post", timeout=60).kwargs["timeout"], 60)
        # ...and an explicit None (deliberate "wait forever") is still honoured.
        self.assertIsNone(self._call("get", timeout=None).kwargs["timeout"])

    def test_user_agent_still_applied(self):
        headers = self._call("get").kwargs["headers"]
        self.assertEqual(headers["User-Agent"], net.USER_AGENT)

    def test_caller_headers_and_ua_coexist(self):
        headers = self._call("get", headers={"X-Bugzilla-API-Key": "k"}).kwargs["headers"]
        self.assertEqual(headers["X-Bugzilla-API-Key"], "k")
        self.assertEqual(headers["User-Agent"], net.USER_AGENT)

    def test_explicit_user_agent_wins(self):
        headers = self._call("get", headers={"User-Agent": "mine"}).kwargs["headers"]
        self.assertEqual(headers["User-Agent"], "mine")


class TestGateSurvivesASlowHg(unittest.TestCase):
    """The path that motivated the timeout: a hanging hg must degrade to "timing unknown",
    not hang the run. sigage swallows the error, so the gate simply no-ops."""

    def test_pushdate_lookup_returns_none_on_timeout(self):
        import requests

        from crashclouseau import sigage
        sigage._JSON_REV_CACHE.clear()
        with mock.patch("requests.get", side_effect=requests.exceptions.ReadTimeout("slow")):
            self.assertIsNone(sigage.pushdate_for_node("c90adbc8b3bf", "nightly"))
            self.assertEqual(sigage.git_commit_for_node("c90adbc8b3bf", "nightly"), "")


if __name__ == "__main__":
    unittest.main()
