"""Authentication routes. Handlers stay thin; behaviour lives in app/services/."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.config import Settings
from app.dependencies.auth import get_optional_session
from app.errors import AppError
from app.logging_config import APP_LOGGER
from app.services import login_attempts
from app.services.accounts import resolve_account
from app.services.cookies import clear_cookie, set_cookie
from app.services.login_attempts import LoginBusy, LoginSecrets
from app.services.oidc import OIDCError, ProviderUnavailable
from app.services.security import is_well_formed_secret
from app.services.sessions import create_session, revoke_session

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(f"{APP_LOGGER}.auth")

# Visible ASCII, bounded. Google's codes are far shorter; anything else is not from Google.
_CODE_FORMAT = re.compile(r"[\x21-\x7e]{1,2048}")

# Stable error codes sent to the frontend error URL as ?error=<code>.
ACCESS_DENIED = "access_denied"
INVALID_LOGIN_ATTEMPT = "invalid_login_attempt"
PROVIDER_UNAVAILABLE = "provider_unavailable"
ACCOUNT_DISABLED = "account_disabled"
LOGIN_FAILED = "login_failed"


# ---- GET /auth/login ------------------------------------------------------------------------


@router.get("/login")
async def login(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    if not settings.oidc_configured:
        raise AppError(503, "oidc_not_configured", "Google login is not configured on this server.")

    secrets_ = LoginSecrets()
    # Discovery first (cached after the first call): if Google is unreachable we fail before
    # writing anything, and no database transaction is ever open during the network call.
    try:
        url = await request.app.state.oidc.authorization_url(
            state=secrets_.state, nonce=secrets_.nonce, code_verifier=secrets_.code_verifier
        )
    except ProviderUnavailable:
        raise AppError(
            503, "provider_unavailable", "Google sign-in is temporarily unavailable."
        ) from None

    try:
        await login_attempts.store_attempt(request.app.state.sessionmaker, settings, secrets_)
    except LoginBusy as busy:
        raise AppError(
            429,
            "login_busy",
            "Too many sign-in attempts are in progress. Try again shortly.",
            headers={"Retry-After": str(busy.retry_after)},
        ) from None

    # The attempt is committed before the browser leaves. One active attempt per browser: the
    # cookie below overwrites any earlier binding cookie, invalidating that earlier attempt.
    response = RedirectResponse(url, status_code=303)
    set_cookie(
        response,
        settings,
        settings.binding_cookie_name,
        secrets_.binding_secret,
        settings.login_attempt_lifetime_seconds,
    )
    return response


# ---- GET /auth/callback ---------------------------------------------------------------------


def _error_redirect(
    settings: Settings, code: str, *, clear_binding: bool = False
) -> RedirectResponse:
    """Every callback failure ends here: a redirect to the FIXED error URL with a stable code.

    The binding cookie is cleared only once the attempt has been consumed. Before that it may
    belong to a still-valid attempt (for example a newer login in another tab), and a stale or
    forged callback must not be able to destroy it.
    """
    parts = urlsplit(settings.frontend_error_url)
    query = [*parse_qsl(parts.query), ("error", code)]
    target = urlunsplit(parts._replace(query=urlencode(query)))
    response = RedirectResponse(target, status_code=303)
    response.headers["Referrer-Policy"] = "no-referrer"
    if clear_binding:
        clear_cookie(response, settings, settings.binding_cookie_name)
    return response


def _provider_error_code(error: str) -> str:
    if error == "access_denied":
        return ACCESS_DENIED
    if error in {"temporarily_unavailable", "server_error"}:
        return PROVIDER_UNAVAILABLE
    return LOGIN_FAILED


@router.get("/callback")
async def callback(request: Request) -> Response:
    """Always responds with a redirect (it is a browser navigation, never a JSON error)."""
    settings: Settings = request.app.state.settings
    try:
        return await _handle_callback(request, settings)
    except Exception as exc:  # last-resort: still a safe redirect, never a stack trace
        log.error("callback crashed: %s", type(exc).__name__)
        return _error_redirect(settings, LOGIN_FAILED, clear_binding=True)


async def _handle_callback(request: Request, settings: Settings) -> Response:
    params = request.query_params

    # 6. Reject early on provider errors and malformed input. No database work yet.
    provider_error = params.get("error")
    if provider_error is not None:
        log.info("provider returned an error response")
        return _error_redirect(settings, _provider_error_code(provider_error))

    states, codes = params.getlist("state"), params.getlist("code")
    if len(states) != 1 or len(codes) != 1:  # missing, or duplicated (parameter pollution)
        return _error_redirect(settings, INVALID_LOGIN_ATTEMPT)
    state, code = states[0], codes[0]
    binding = request.cookies.get(settings.binding_cookie_name)
    if (
        not is_well_formed_secret(state)
        or not _CODE_FORMAT.fullmatch(code)
        or not is_well_formed_secret(binding)
    ):
        return _error_redirect(settings, INVALID_LOGIN_ATTEMPT)
    assert binding is not None

    sessionmaker = request.app.state.sessionmaker

    # Browser's existing ThreadForge session, if any (read in its own short transaction).
    previous = await get_optional_session(request)

    # 7. Atomically validate (state + browser binding + expiry + single use) and consume.
    #    Transaction 1 ends here, before any network call.
    attempt = await login_attempts.consume_attempt(sessionmaker, state, binding)
    if attempt is None:
        return _error_redirect(settings, INVALID_LOGIN_ATTEMPT)

    # 8-9. Talk to Google with NO database transaction open.
    try:
        identity = await request.app.state.oidc.complete_login(
            code=code, code_verifier=attempt.code_verifier, nonce=attempt.nonce
        )
    except OIDCError as exc:
        log.warning("login rejected: %s (%s)", exc.code, exc)
        return _error_redirect(settings, exc.code, clear_binding=True)

    # 10. Resolve the account by (issuer, subject). Transaction 2 (create) / 2' (refresh).
    account = await resolve_account(sessionmaker, identity)
    if account.state != "active":
        log.info("login refused: account not active")
        return _error_redirect(settings, ACCOUNT_DISABLED, clear_binding=True)

    # 11. Transaction 3: fresh session; revoke the session this browser presented, if valid.
    #     Both happen atomically, so we never end up with zero or two live sessions.
    async with sessionmaker() as db, db.begin():
        new_session = await create_session(db, settings, account.user_id)
        if previous is not None:
            await revoke_session(db, previous.session_id)

    # 12. Clear the one-shot binding cookie, set the session cookie, go to the fixed success URL.
    response = RedirectResponse(settings.frontend_success_url, status_code=303)
    response.headers["Referrer-Policy"] = "no-referrer"
    clear_cookie(response, settings, settings.binding_cookie_name)
    set_cookie(
        response,
        settings,
        settings.session_cookie_name,
        new_session.secret,
        settings.session_lifetime_seconds,
    )
    return response
