# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Mozilla Google sign-in grants access to withheld analyses, not write routes.

Requires SECRET_KEY, GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.
"""

import time
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth
from flask import abort, redirect, request, session, url_for

from crashclouseau import app
from .logger import logger

ALLOWED_DOMAIN = "mozilla.com"
# Google documents both issuer spellings for its ID tokens.
_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]

_oauth = OAuth(app)
_oauth.register(
    "google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
)


def enabled() -> bool:
    cfg = app.config
    return bool(app.secret_key and cfg.get("GOOGLE_CLIENT_ID") and cfg.get("GOOGLE_CLIENT_SECRET"))


def _allowed(claims) -> bool:
    """Require mozilla.com organization membership and a verified mozilla.com email.

    Google's ``hd`` claim identifies the organization; the email domain alone does not.
    """
    email = (claims.get("email") or "").lower()
    verified = claims.get("email_verified") is True
    return claims.get("hd") == ALLOWED_DOMAIN and verified and email.endswith("@" + ALLOWED_DOMAIN)


def current_user():
    """Return the session user if sign-in is enabled and the login is unexpired.

    Expiry is measured from ``signed_in_at``.
    """
    if not enabled():
        return None
    user = session.get("user")
    at = session.get("signed_in_at")
    if not isinstance(user, dict) or not isinstance(at, int):
        return None
    if time.time() - at > app.permanent_session_lifetime.total_seconds():
        return None
    return user


def _safe_next(target) -> str:
    """Return a local path or ``/``. Reject backslashes and controls to avoid URL normalization."""
    if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(c) < 0x20 for c in target):
        return "/"
    parts = urlsplit(target)
    return "/" if parts.scheme or parts.netloc else target


def login():
    if not enabled():
        abort(503, "sign-in is not configured")
    session["next"] = _safe_next(request.args.get("next"))
    # The request's `hd` is a UI hint; callback() checks the signed claim.
    return _oauth.google.authorize_redirect(
        url_for("auth_callback", _external=True), hd=ALLOWED_DOMAIN, prompt="select_account")


def callback():
    if not enabled():
        abort(503, "sign-in is not configured")
    try:
        # Authlib 1.8.0 accepts a wrong aud with matching azp without this aud constraint.
        token = _oauth.google.authorize_access_token(claims_options={
            "iss": {"essential": True, "values": _ISSUERS},
            "aud": {"essential": True, "value": app.config["GOOGLE_CLIENT_ID"]},
        })
        claims = token.get("userinfo") or {}
    except Exception:
        logger.warning("sign-in: token exchange or ID token validation failed", exc_info=True)
        claims = None
    next_ = _safe_next(session.get("next"))
    session.clear()
    if claims is None:
        abort(400, "sign-in failed")
    if not _allowed(claims):
        logger.info("sign-in refused: hd=%r, email domain %r", claims.get("hd"),
                    (claims.get("email") or "").rpartition("@")[2])
        abort(403, "only {} Google accounts can sign in".format(ALLOWED_DOMAIN))
    session.permanent = True
    picture = claims.get("picture")
    session["user"] = {
        "email": claims["email"].lower(),
        "name": claims.get("name") or "",
        "picture": picture if isinstance(picture, str) and picture.startswith("https://") else "",
    }
    session["signed_in_at"] = int(time.time())
    return redirect(next_)


def logout():
    if app.secret_key:
        session.clear()
    return redirect(_safe_next(request.form.get("next")))
