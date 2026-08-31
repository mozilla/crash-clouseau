# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""``/api/tasks/retrigger`` spends money per call, so it must be authenticated.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_retrigger_auth

It was ``@cross_origin()`` POST with no authorization of any kind from the day it landed until
2026-08-31. Measured on the deployed app at v139: one anonymous ``GET /tasks.html`` returns 500
real uuids with ``retriggerTask('<uuid>')`` already wired; 3,188 of 3,488 ``uuids`` rows carry the
``crashstack`` rows ``build_seed`` needs; production runs cost a mean $1.70 and a maximum $8.49;
there is no rate limit and no global spend cap (``max_cost_usd_per_crash`` warns, it does not
abort). ``_require_write_token`` had been written for the NEIGHBOURING route at ``40d678f`` and
this one was never given it.

``grep -rn retrigger tests/`` had 20+ hits before this file and not one asserted authorization.
That is the gap these tests close: the assertion is trivial, remembering it is the hard part.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import api, app, models                                # noqa: E402
from crashclouseau.agent import orchestrator                              # noqa: E402

_UUID = "0d1e2f3a-4b5c-6d7e-8f90-a1b2c3d4e5f6"
_TOKEN = "s3cret-viewer-token"


class _Recorder:
    """Stands in for `retrigger_agent` so a passing auth check costs nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, uuid):
        self.calls.append(uuid)
        return {"uuid": uuid, "cancelled": False, "already_filed": None}


class TestTheRouteRefusesAnonymously(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.recorder = _Recorder()
        # Patched at the orchestrator, i.e. BELOW the gate: if the gate leaks, the recorder
        # sees the call. Asserting only on the status code would pass for a route that 403s
        # after enqueuing.
        self.p_retrigger = mock.patch.object(
            orchestrator, "retrigger_agent", self.recorder)
        self.p_exists = mock.patch.object(models.UUID, "exists", return_value=True)
        self.p_retrigger.start()
        self.p_exists.start()
        self.addCleanup(self.p_retrigger.stop)
        self.addCleanup(self.p_exists.stop)

    def _post(self, **kwargs):
        return self.client.post("/api/tasks/retrigger", json={"uuid": _UUID}, **kwargs)

    def test_anonymous_post_is_refused_and_enqueues_nothing(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False):
            rv = self._post()
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(self.recorder.calls, [], "the gate leaked: a run was triggered")

    def test_a_wrong_token_is_refused(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False):
            rv = self._post(headers={"X-Clouseau-Token": "not-it"})
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(self.recorder.calls, [])

    def test_an_unset_secret_refuses_rather_than_opening_the_route(self):
        """The direction that matters. An unset secret must never read as "no auth required" --
        that is how the neighbouring route's docstring puts it, and it is why this asserts on
        the empty string rather than on a missing key."""
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": ""}, clear=False):
            rv = self._post()
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(self.recorder.calls, [])


class TestTheOperatorCanStillUseIt(unittest.TestCase):
    """The fix must not close the hole by breaking the tasks-view button.

    ``static/clouseau.js`` sends no ``X-Clouseau-Token`` and cannot: ``VIEW_COOKIE`` is
    ``httponly``, so script cannot read the token to attach it. Gating on
    ``_require_write_token`` (header-only) would have made the button 403 forever with no
    server-side error -- so these three arms are the whole reason `_require_viewer` exists."""

    def setUp(self):
        self.client = app.test_client()
        self.recorder = _Recorder()
        self.p_retrigger = mock.patch.object(
            orchestrator, "retrigger_agent", self.recorder)
        self.p_exists = mock.patch.object(models.UUID, "exists", return_value=True)
        self.p_retrigger.start()
        self.p_exists.start()
        self.addCleanup(self.p_retrigger.stop)
        self.addCleanup(self.p_exists.stop)

    def test_the_header_works(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False):
            rv = self.client.post("/api/tasks/retrigger", json={"uuid": _UUID},
                                  headers={"X-Clouseau-Token": _TOKEN})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.recorder.calls, [_UUID])

    def test_the_cookie_works_because_the_browser_button_has_only_that(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False):
            self.client.set_cookie(api.VIEW_COOKIE, _TOKEN, domain="localhost")
            rv = self.client.post("/api/tasks/retrigger", json={"uuid": _UUID})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.recorder.calls, [_UUID])

    def test_a_query_arg_token_works(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False):
            rv = self.client.post(f"/api/tasks/retrigger?token={_TOKEN}",
                                  json={"uuid": _UUID})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.recorder.calls, [_UUID])


class TestTheCSRFShapeIsGone(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.recorder = _Recorder()
        self.p_retrigger = mock.patch.object(
            orchestrator, "retrigger_agent", self.recorder)
        self.p_retrigger.start()
        self.addCleanup(self.p_retrigger.stop)

    def test_a_query_string_uuid_no_longer_triggers_a_run(self):
        """The uuid must come from a JSON body.

        A query-string uuid made this reachable by a plain cross-site HTML form POST -- the one
        request shape that carries cookies with no preflight. ``VIEW_COOKIE`` is
        ``samesite="Lax"`` so that is closed today, but by one keyword in an unrelated function;
        requiring the body forces a preflight instead."""
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False), \
                mock.patch.object(models.UUID, "exists", return_value=True):
            self.client.set_cookie(api.VIEW_COOKIE, _TOKEN, domain="localhost")
            rv = self.client.post(f"/api/tasks/retrigger?uuid={_UUID}")
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.recorder.calls, [])

    def test_the_route_does_not_echo_an_arbitrary_origin(self):
        """The bare ``@cross_origin()`` echoed whatever ``Origin`` it was sent -- verified
        against the deployed app with ``Origin: https://evil.example``. A route that spends
        money must not be readable cross-site."""
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False):
            rv = self.client.post("/api/tasks/retrigger", json={"uuid": _UUID},
                                  headers={"Origin": "https://evil.example"})
        self.assertNotEqual(
            rv.headers.get("Access-Control-Allow-Origin"), "https://evil.example")


class TestAnUnknownUuidIs404NotA500(unittest.TestCase):
    """``UUID.get_id`` does ``...first()[0]`` on ``None``, so an unknown uuid used to raise
    ``TypeError`` and return a 500. On a web dyno with ONE gunicorn worker and no ``-w``, an
    unauthenticated 500 is a free way to burn request capacity."""

    def test_unknown_uuid(self):
        client = app.test_client()
        recorder = _Recorder()
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": _TOKEN}, clear=False), \
                mock.patch.object(orchestrator, "retrigger_agent", recorder), \
                mock.patch.object(models.UUID, "exists", return_value=False):
            rv = client.post("/api/tasks/retrigger", json={"uuid": "nope"},
                             headers={"X-Clouseau-Token": _TOKEN})
        self.assertEqual(rv.status_code, 404)
        self.assertEqual(recorder.calls, [])

    def test_get_id_still_raises_for_the_twelve_internal_callers(self):
        """Deliberately NOT softened to return None: twelve callers in ``models`` do
        ``uuidid = UUID.get_id(uuid)`` and then filter on it, and ``None`` would match no rows
        instead of raising -- turning "no such crash" into "no dossier for this crash"."""
        import inspect
        src = inspect.getsource(models.UUID.get_id)
        self.assertIn("first()[0]", src)


if __name__ == "__main__":
    unittest.main()
