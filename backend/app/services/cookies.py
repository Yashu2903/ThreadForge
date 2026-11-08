"""Cookie attributes in one place so setting and deleting always use matching attributes.

Both cookies we issue (session, login binding) are: HttpOnly, Path=/, SameSite=Lax, no Domain,
and Secure whenever settings.secure_cookies (always in production). In that case their names carry
the __Host- prefix, which makes browsers *enforce* Secure + Path=/ + no Domain.

SameSite=Lax (not Strict) is deliberate: the login binding cookie must be sent when Google
redirects the browser back to /auth/callback, which is a cross-site top-level navigation.
"""

from __future__ import annotations

from starlette.responses import Response

from app.config import Settings


def set_cookie(response: Response, settings: Settings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path="/",
        domain=None,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response, settings: Settings, name: str) -> None:
    # Deleting must repeat the attributes used to set it, or the browser may keep the cookie.
    response.delete_cookie(
        key=name,
        path="/",
        domain=None,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
