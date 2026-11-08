"""CSRF protection for state-changing, cookie-authenticated requests.

Cookies are attached by the browser automatically, even to requests a malicious site triggers.
So a cookie alone cannot prove the request came from our frontend. We require two more things:

  1. A CSRF token in the X-CSRF-Token header. A cross-site page cannot read it (it is only
     returned to authenticated same-origin/CORS-permitted callers via GET /auth/csrf) and cannot
     compute it (it needs CSRF_SECRET *and* the session secret).
  2. An Origin header that exactly matches a trusted origin. Browsers set Origin on cross-site
     POSTs and scripts cannot forge it.

Neither CORS nor SameSite is relied on as the CSRF defence. They help, but SameSite has
exceptions and CORS only governs who may *read* responses.

How future mutating routes use this: the frontend calls GET /auth/csrf once after login (and
again if it gets 403 csrf_failed), keeps the token in memory (never localStorage), and sends it
as `X-CSRF-Token` on every POST/PUT/PATCH/DELETE together with the session cookie. Server side,
a route calls enforce_csrf(request, settings, auth_context) before doing any work.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request

from app.config import Settings
from app.errors import AppError
from app.services.sessions import AuthContext

CSRF_HEADER = "x-csrf-token"
_CONTEXT_PREFIX = "csrf-v1:"


def csrf_token_for(settings: Settings, session_secret: str) -> str:
    key = settings.csrf_secret.get_secret_value().encode("utf-8")
    message = (_CONTEXT_PREFIX + session_secret).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def enforce_csrf(request: Request, settings: Settings, context: AuthContext) -> None:
    """Raise 403 csrf_failed unless Origin is trusted and the CSRF token is valid.

    The Origin header is REQUIRED: this API is browser-only in this part, and every browser
    sends Origin on cross-site POSTs. (A non-browser client would have no use for cookies anyway.)
    The response is identical for every failure so it teaches an attacker nothing.
    """
    origin = request.headers.get("origin")
    supplied = request.headers.get(CSRF_HEADER, "")
    expected = csrf_token_for(settings, context.session_secret)

    origin_ok = origin is not None and origin.lower() in settings.trusted_origins
    token_ok = hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    if not (origin_ok and token_ok):
        raise AppError(403, "csrf_failed", "The request could not be verified.")
