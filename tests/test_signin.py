# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Google sign-in (``crashclouseau.auth``) and what it unlocks.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_signin

Callback tests use Authlib validation and locally signed tokens with mocked Google metadata
and token exchange. They do not contact Google.
"""
import os
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from joserfc import jwt                                                   # noqa: E402
from joserfc.jwk import RSAKey                                            # noqa: E402

from crashclouseau import api, app, auth, html, models                    # noqa: E402
from crashclouseau.agent import orchestrator                              # noqa: E402
from tests.test_tasks_view import _spike                                  # noqa: E402

_CLIENT_ID = "test-client.apps.googleusercontent.com"
_CONFIG = {"SECRET_KEY": "test-secret", "GOOGLE_CLIENT_ID": _CLIENT_ID,
           "GOOGLE_CLIENT_SECRET": "test-client-secret"}
_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})
_METADATA = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks": {"keys": [_KEY.as_dict(private=False)]},
    "id_token_signing_alg_values_supported": ["RS256"],
}
_PICTURE = "https://lh3.googleusercontent.com/a/abc=s96-c"
_MOZ = {"email": "dev@mozilla.com", "email_verified": True, "hd": "mozilla.com",
        "name": "A Developer", "picture": _PICTURE}
# No recorded disclosure screen, so anonymous viewers cannot see these findings.
_WITHHELD_SPIKE = _spike(filing=None, findings={"assessment": "regression",
                                                "culprit": {"node": "0a49d5b304b4"}})


def _id_token(nonce, claims):
    now = int(time.time())
    body = {"iss": "https://accounts.google.com", "aud": _CLIENT_ID, "sub": "1234",
            "iat": now, "exp": now + 3600, "nonce": nonce}
    body.update(claims)
    return jwt.encode({"alg": "RS256", "kid": "k1"}, body, _KEY)


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        google = auth._oauth.google
        for patcher in (mock.patch.dict(app.config, _CONFIG),
                        mock.patch.object(google, "client_id", _CLIENT_ID),
                        mock.patch.object(google, "load_server_metadata",
                                          return_value=_METADATA)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _login(self, next_="/tasks.html"):
        rv = self.client.get("/login", query_string={"next": next_})
        self.assertEqual(rv.status_code, 302)
        return rv

    def _callback(self, token_claims=None, state=None):
        """Run /login, then /login/callback with an ID token carrying ``token_claims``."""
        query = parse_qs(urlsplit(self._login().location).query)
        id_token = _id_token(query["nonce"][0], token_claims or {})
        token = {"access_token": "at", "token_type": "Bearer", "expires_in": 3600,
                 "id_token": id_token}
        with mock.patch.object(auth._oauth.google, "fetch_access_token", return_value=token):
            return self.client.get("/login/callback",
                                   query_string={"code": "c", "state": state or query["state"][0]})

    def _sign_in(self, email="dev@mozilla.com", at=None, name="", picture=""):
        with self.client.session_transaction() as s:
            s["user"] = {"email": email, "name": name, "picture": picture}
            s["signed_in_at"] = int(time.time()) if at is None else at

    def _signed_in(self):
        with self.client.session_transaction() as s:
            return s.get("user")


class TestTheLoginRedirect(_Base):
    def test_it_asks_google_for_a_mozilla_account(self):
        rv = self._login()
        url = urlsplit(rv.location)
        query = parse_qs(url.query)
        self.assertEqual(url.netloc, "accounts.google.com")
        self.assertEqual(query["hd"], ["mozilla.com"])
        self.assertEqual(query["client_id"], [_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], ["http://localhost/login/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("nonce", query)
        self.assertIn("openid", query["scope"][0].split())

    def test_the_redirect_uri_is_https_behind_the_router(self):
        rv = self.client.get("/login", headers={"X-Forwarded-Proto": "https"})
        query = parse_qs(urlsplit(rv.location).query)
        self.assertEqual(query["redirect_uri"], ["https://localhost/login/callback"])

    def test_unconfigured_sign_in_is_refused(self):
        for key in _CONFIG:
            with self.subTest(unset=key), mock.patch.dict(app.config, {key: ""}):
                self.assertEqual(self.client.get("/login").status_code, 503)
                self.assertEqual(self.client.get("/login/callback").status_code, 503)


class TestTheCallback(_Base):
    def test_a_mozilla_account_is_signed_in_and_returned_to_next(self):
        rv = self._callback(_MOZ)
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(rv.location, "/tasks.html")
        self.assertEqual(self._signed_in(), {"email": "dev@mozilla.com", "name": "A Developer",
                                             "picture": _PICTURE})

    def test_only_an_https_picture_is_kept(self):
        for picture in ("http://example.com/a.png", "javascript:alert(1)", ["x"]):
            with self.subTest(picture=picture):
                self._callback(dict(_MOZ, picture=picture))
                self.assertEqual(self._signed_in()["picture"], "")

    def test_other_accounts_are_refused(self):
        cases = {
            "consumer account with a mozilla.com address": dict(_MOZ, hd=None),
            "other workspace": dict(_MOZ, hd="example.com", email="dev@example.com"),
            "gmail": {"email": "dev@gmail.com", "email_verified": True},
            "unverified email": dict(_MOZ, email_verified=False),
            "hd without a matching email": dict(_MOZ, email="dev@example.com"),
        }
        for name, claims in cases.items():
            with self.subTest(name):
                claims = {k: v for k, v in claims.items() if v is not None}
                rv = self._callback(claims)
                self.assertEqual(rv.status_code, 403)
                self.assertIsNone(self._signed_in())

    def test_a_refused_sign_in_clears_an_existing_one(self):
        self._callback(_MOZ)
        self.assertEqual(self._callback({"email": "dev@gmail.com"}).status_code, 403)
        self.assertIsNone(self._signed_in())

    def test_invalid_id_tokens_are_refused(self):
        cases = {
            "wrong audience": dict(_MOZ, aud="someone-else"),
            "wrong audience with our client as azp": dict(_MOZ, aud="someone-else",
                                                          azp=_CLIENT_ID),
            "wrong issuer": dict(_MOZ, iss="https://evil.example"),
            "expired": dict(_MOZ, exp=int(time.time()) - 3600, iat=int(time.time()) - 7200),
            "wrong nonce": dict(_MOZ, nonce="not-the-nonce"),
        }
        for name, claims in cases.items():
            with self.subTest(name):
                self.assertEqual(self._callback(claims).status_code, 400)
                self.assertIsNone(self._signed_in())

    def test_a_callback_without_a_matching_state_is_refused(self):
        self.assertEqual(self._callback(_MOZ, state="forged").status_code, 400)
        self.assertIsNone(self._signed_in())
        self.assertEqual(self.client.get("/login/callback?code=c&state=x").status_code, 400)

    def test_google_reporting_an_error_is_refused(self):
        self._login()
        rv = self.client.get("/login/callback?error=access_denied")
        self.assertEqual(rv.status_code, 400)


class TestNext(unittest.TestCase):
    def test_only_local_paths_are_followed(self):
        for target, expected in (
                ("/crashstack.html?uuid=u", "/crashstack.html?uuid=u"),
                ("/", "/"),
                (None, "/"),
                ("", "/"),
                ("https://evil.example/", "/"),
                ("//evil.example/", "/"),
                ("/\\evil.example/", "/"),
                ("/\t/evil.example/", "/"),
                ("/\n/evil.example/", "/"),
                ("evil.example", "/"),
                ("javascript:alert(1)", "/")):
            with self.subTest(target=target):
                self.assertEqual(auth._safe_next(target), expected)


class TestTheSession(_Base):
    def _authorized(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": ""}, clear=False):
            with mock.patch.object(html.models.SpikeEscalation, "recent",
                                   return_value=[dict(_WITHHELD_SPIKE)]):
                row = self.client.get("/api/spikes").get_json()["rows"][0]
        return row["findings"] is not None

    def test_anonymous_viewers_do_not_see_withheld_findings(self):
        self.assertFalse(self._authorized())

    def test_a_signed_in_user_sees_them_without_the_token(self):
        self._sign_in()
        self.assertTrue(self._authorized())

    def test_the_session_expires_from_sign_in(self):
        lifetime = int(app.permanent_session_lifetime.total_seconds())
        self._sign_in(at=int(time.time()) - lifetime - 60)
        self.assertFalse(self._authorized())

    def test_unconfiguring_sign_in_disables_session_access(self):
        self._sign_in()
        with mock.patch.dict(app.config, {"GOOGLE_CLIENT_ID": ""}):
            self.assertFalse(self._authorized())

    def test_a_session_signed_with_another_key_is_ignored(self):
        self._sign_in()
        with mock.patch.dict(app.config, {"SECRET_KEY": "rotated"}):
            self.assertFalse(self._authorized())

    def test_sign_out(self):
        self._sign_in()
        rv = self.client.post("/logout", data={"next": "/tasks.html"})
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(rv.location, "/tasks.html")
        self.assertFalse(self._authorized())

    def test_sign_out_is_post_only(self):
        self._sign_in()
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertTrue(self._authorized())

    def test_the_evidence_route_serves_the_full_dossier(self):
        from crashclouseau import bugzilla_apply
        self._sign_in()
        with mock.patch.object(bugzilla_apply, "build_evidence", return_value=None) as build:
            self.client.get("/api/evidence?uuid=u-1")
        build.assert_called_once_with("u-1", public=False)

    def test_sign_in_does_not_grant_retrigger(self):
        self._sign_in()
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}, clear=False), \
                mock.patch.object(orchestrator, "retrigger_agent") as run, \
                mock.patch.object(models.UUID, "exists", return_value=True):
            rv = self.client.post("/api/tasks/retrigger", json={"uuid": "u-1"})
        self.assertEqual(rv.status_code, 403)
        run.assert_not_called()

    def test_the_token_still_works(self):
        with mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}, clear=False):
            with app.test_request_context("/", headers={"X-Clouseau-Token": "s3cret"}):
                self.assertTrue(api.viewer_authorized())
            with app.test_request_context("/"):
                self.assertFalse(api.viewer_authorized())


class TestThePages(_Base):
    def _tasks(self):
        with mock.patch.object(html.models.Dossier, "list_tasks", return_value=[]), \
                mock.patch.object(html.models.SpikeEscalation, "recent",
                                  return_value=[dict(_WITHHELD_SPIKE)]):
            rv = self.client.get("/tasks.html")
        self.assertEqual(rv.status_code, 200)
        return rv.get_data(as_text=True)

    def test_anonymous(self):
        body = self._tasks()
        self.assertIn('href="/login?next=/tasks.html"', body)
        self.assertIn("assess-withheld", body)
        self.assertNotIn("0a49d5b304b4", body)

    def test_signed_in(self):
        self._sign_in(name="A Developer", picture=_PICTURE)
        body = self._tasks()
        self.assertIn('<img class="avatar" src="{}"'.format(_PICTURE), body)
        self.assertIn('referrerpolicy="no-referrer"', body)
        self.assertIn('title="dev@mozilla.com">A Developer</span>', body)
        self.assertIn('action="/logout"', body)
        self.assertNotIn("assess-withheld", body)
        self.assertIn("0a49d5b304b4", body)

    def test_signed_in_without_a_name_or_picture(self):
        self._sign_in()
        body = self._tasks()
        self.assertNotIn('class="avatar"', body)
        self.assertIn('title="dev@mozilla.com">dev@mozilla.com</span>', body)

    def test_no_sign_in_link_when_unconfigured(self):
        with mock.patch.dict(app.config, {"GOOGLE_CLIENT_ID": ""}):
            self.assertNotIn("/login?", self._tasks())


if __name__ == "__main__":
    unittest.main()
