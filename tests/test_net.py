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


def _repo_file(path):
    """Read a repo file relative to the checkout root (tests run from there)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, path), encoding="utf-8") as f:
        return f.read()


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


class TestLibmozdataIsBounded(unittest.TestCase):
    """libmozdata's own defaults, which `crashclouseau.net` pins on import. Left alone they
    are `TIMEOUT = 30` (the whole budget gunicorn gives a request) and `MAX_RETRIES = 256`
    with `backoff_factor=1` (~8.5 hours of retrying on ONE request). Together they took
    crashstack.html down on 2026-08-25 when a `/rest/bug` read stalled: gunicorn's abort is a
    `SystemExit` raised inside the blocked thread, so none of the page's `except Exception`
    best-effort guards could turn it back into "render without the preview"."""

    # Heroku's router gives up on a request at 30s and returns H12, whatever gunicorn is
    # configured to allow. So a single stalled upstream has to fail with time left to render.
    _ROUTER_TIMEOUT_S = 30

    def _classes(self):
        from libmozdata.bugzilla import Bugzilla, BugzillaUser
        from libmozdata.socorro import SuperSearch
        return (Bugzilla, BugzillaUser, SuperSearch)

    def _gunicorn_timeout(self):
        import re
        proc = _repo_file("Procfile")
        m = re.search(r"^web:.*?--timeout\s+(\d+)", proc, re.M)
        self.assertIsNotNone(m, "the web dyno must set an explicit gunicorn --timeout")
        return int(m.group(1))

    def test_retries_are_bounded(self):
        from libmozdata.connection import Connection
        # urllib3 sleeps backoff_factor * 2**(n-1), capped at DEFAULT_BACKOFF_MAX (120s).
        self.assertLessEqual(Connection.MAX_RETRIES, 4)

    def test_web_path_services_are_tightened(self):
        # Every class the page actually talks to inherits the bound (none define their own).
        for cls in self._classes():
            with self.subTest(cls=cls.__name__):
                self.assertEqual(len(cls.TIMEOUT), 2, "want a (connect, read) pair")
                connect, read = cls.TIMEOUT
                self.assertGreater(connect, 0)
                self.assertGreater(read, 0)

    def test_one_stalled_upstream_still_leaves_time_to_render(self):
        from libmozdata.connection import Connection
        attempts = Connection.MAX_RETRIES + 1
        backoff = sum(min(120, 2 ** (n - 1)) for n in range(1, Connection.MAX_RETRIES + 1))
        for cls in self._classes():
            worst = attempts * cls.TIMEOUT[1] + backoff
            with self.subTest(cls=cls.__name__, worst=worst):
                # ...before gunicorn SIGABRTs the worker (which defeats the guards), and
                # before the router H12s (which loses the page even if the worker survives).
                self.assertLess(worst, self._gunicorn_timeout())
                self.assertLess(worst, self._ROUTER_TIMEOUT_S)

    def test_hg_keeps_its_loose_read_timeout(self):
        # NOT tightened on purpose: hg.mozilla.org takes 8-13s to answer a json-annotate at
        # the best of times, and the agent's blame/history/patch tools lean on it hard. A
        # single global bound sized for BMO would fail those calls, and the agent has no
        # cheaper way to get pinned blame.
        from libmozdata.hgmozilla import Annotate, FileInfo, RawRevision, Revision
        for cls in (Annotate, FileInfo, RawRevision, Revision):
            with self.subTest(cls=cls.__name__):
                read = cls.TIMEOUT[1] if isinstance(cls.TIMEOUT, tuple) else cls.TIMEOUT
                self.assertGreaterEqual(read, 30)

    def test_the_bound_comes_from_importing_net(self):
        # The knobs are class attributes on libmozdata's base classes and nothing else in the
        # tree sets them, so this asserts `net`'s import-time patch is what applied them:
        # revert it and every assertion above reads libmozdata's own 30 / 256.
        import ast
        tree = ast.parse(_repo_file("crashclouseau/net.py"))
        targets = {
            t.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Attribute)
        }
        self.assertIn("MAX_RETRIES", targets)
        self.assertIn("TIMEOUT", targets)


class TestPreviewSurvivesASlowBugzilla(unittest.TestCase):
    """The 2026-08-25 page outage, end to end. `_bug_meta` and `resolve_product_component` are
    both documented "best-effort, never raises", and they always were -- for BMO *errors*. A
    stalled read never reached them at all: it ran until gunicorn aborted the worker, and that
    arrives as SystemExit, which `except Exception` cannot catch. Bounded, the guards work and
    the page renders with an empty product/component instead of 500ing."""

    def _timing_out(self):
        import requests
        return mock.patch("requests.adapters.HTTPAdapter.send",
                          side_effect=requests.exceptions.ReadTimeout("BMO is quiet"))

    def test_bug_metadata_degrades_to_absent(self):
        from crashclouseau import report_bug
        report_bug._BUG_CACHE.clear()
        with self._timing_out():
            self.assertEqual(report_bug._bug_meta([2048793]), {})
        # "Could not ASK" must NOT be cached as "unreadable bug": caching it would blind this
        # process to that bug for the life of the dyno (report_bug._bug_meta).
        self.assertEqual(dict(report_bug._BUG_CACHE), {})

    def test_preview_resolves_to_no_component_instead_of_raising(self):
        from crashclouseau import report_bug
        report_bug._BUG_CACHE.clear()
        # `bug` with no `node`/`author` keeps this to rung 1, i.e. no DB.
        with self._timing_out():
            self.assertEqual(
                report_bug.resolve_product_component({"bug": 2048793}, "nightly", "Firefox"),
                (None, None))


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
