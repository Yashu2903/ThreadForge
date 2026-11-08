"""Authentication dependencies.

Authentication answers "who is this?". Part 2 will add authorization, "what may they access?":
every future room route will depend on get_current_user first and only then check membership.
Authorization can never run without an authenticated user.

The lookups use their own short-lived database session, so no connection or transaction stays open
after the dependency returns (important for the OIDC callback, which then calls Google).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.errors import AppError
from app.services.sessions import AuthContext, load_session


async def get_optional_session(request: Request) -> AuthContext | None:
    """The validated session for this request's cookie, or None.

    Never raises for a missing, malformed, unknown, expired or revoked cookie. Used by logout and
    by login rotation, which must work with or without a prior session.
    """
    settings = request.app.state.settings
    secret = request.cookies.get(settings.session_cookie_name)
    if not secret:
        return None
    async with request.app.state.sessionmaker() as db:
        return await load_session(db, secret)


async def get_current_user(request: Request) -> AuthContext:
    """Require a valid session whose user is active.

    A disabled or deleted user gets the same 401, code and message as a missing session, so the
    response never reveals that the account exists or what state it is in.
    """
    context = await get_optional_session(request)
    if context is None or context.user.state != "active":
        raise AppError(401, "unauthenticated", "Authentication required.")
    return context


# Annotated aliases so a route declares its auth requirement in one word:
#     async def route(user: CurrentUser): ...
CurrentUser = Annotated[AuthContext, Depends(get_current_user)]
OptionalSession = Annotated[AuthContext | None, Depends(get_optional_session)]
