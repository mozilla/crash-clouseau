# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""``/api/javast`` and the webextension are retired.

    DATABASE_URL=sqlite:// uv run python -m unittest tests.test_javast_retired

It was the only unauthenticated, `origins: "*"` route, and it 500ed on EVERY request carrying a
non-empty stack: `models.Build.get_changeset` compared a JSON-string buildid against a
`timestamptz` column AND filtered on product `FennecAndroid`, which `a1888ce` dropped from
`config/global.json` in 2022. So it could not have answered even with the SQL repaired -- there
is no Android product in the database. CI never noticed because `tests/test_java.py` injects its
own `get_changeset` and nothing exercised the route.

These assertions are cheap; the thing worth pinning is that re-adding the route silently would
restore an anonymous cross-origin surface.
"""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from crashclouseau import api, app                                        # noqa: E402


class JavastRetiredTest(unittest.TestCase):
    def test_the_route_is_gone(self):
        rules = {r.rule for r in app.url_map.iter_rules()}
        self.assertNotIn("/api/javast", rules)

    def test_posting_to_it_is_a_404(self):
        resp = app.test_client().post(
            "/api/javast",
            json={"channel": "nightly", "buildid": "20260829211045", "stack": "at a.B.c(B.java:1)"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_no_handler_is_left_behind(self):
        self.assertFalse(hasattr(api, "javast"))

    def test_cors_is_not_granted_app_wide(self):
        # The webextension was the only cross-origin consumer, so the resource set is now empty
        # and `CORS(app)` must never be widened back to app-wide: that attaches a permissive,
        # Origin-echoing header to EVERY route. A view carrying no `@cross_origin()` is the
        # probe -- `/api/tasks/retrigger`, the one route that spends money per call.
        # (NB six views DO still carry a bare `@cross_origin()` and grant CORS independently;
        # that is pre-existing and documented in `crashclouseau/__init__.py`.)
        resp = app.test_client().post(
            "/api/tasks/retrigger", headers={"Origin": "https://evil.example"}, json={}
        )
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

    def test_the_webextension_is_gone(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(root, "webextension")))


if __name__ == "__main__":
    unittest.main()
