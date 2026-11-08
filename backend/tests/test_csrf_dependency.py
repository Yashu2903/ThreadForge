"""Structural guarantees: CSRF on every unsafe route, and Part 2 routes never write tables.

Routers are discovered from the app.api package (not from app.routes: this FastAPI version wraps
included routers in private lazy objects), so a router added later is covered automatically.
"""

import importlib
import pkgutil
import re
from pathlib import Path

from fastapi.routing import APIRoute

import app.api
from app.dependencies.auth import get_current_user, require_csrf

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Logout must answer 204 when there is no session at all, so it calls enforce_csrf itself, only
# when a session exists (covered by test_logout.py). Nothing else may be exempt.
CSRF_EXEMPT = {("POST", "/auth/logout")}
PART2_ROUTE_MODULES = ("rooms.py", "invitations.py", "members.py")


def _api_routes() -> list[APIRoute]:
    found = []
    for module_info in pkgutil.iter_modules(app.api.__path__):
        router = getattr(importlib.import_module(f"app.api.{module_info.name}"), "router", None)
        if router is not None:
            found += [r for r in router.routes if isinstance(r, APIRoute)]
    return found


def _calls(dependant) -> set:
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def test_every_unsafe_route_depends_on_require_csrf():
    unsafe = [(m, r.path, r) for r in _api_routes() for m in r.methods & UNSAFE]
    assert len(unsafe) >= 2, "vacuous: found no unsafe routes to check"
    for method, path, route in unsafe:
        if (method, path) in CSRF_EXEMPT:
            continue
        calls = _calls(route.dependant)
        assert require_csrf in calls, f"{method} {path} is missing require_csrf"
        assert get_current_user in calls, f"{method} {path} is missing authentication"


def test_every_non_public_route_requires_authentication():
    public = {
        "/health/live",
        "/health/ready",
        "/auth/login",
        "/auth/callback",
        "/auth/logout",  # optional session by design
    }
    routes = _api_routes()
    assert any(r.path.startswith("/rooms") for r in routes)
    for route in routes:
        if route.path not in public:
            assert get_current_user in _calls(route.dependant), route.path


def test_part2_route_modules_never_write_database_tables_directly():
    """Writes to rooms/memberships/invitations live only in app/services/, so Part 4 can add its
    room-event/outbox writes in the same transactions in one place."""
    for name in PART2_ROUTE_MODULES:
        path = Path("app/api") / name
        if not path.exists():
            continue  # invitations.py arrives in checkpoint 3
        source = path.read_text()
        assert not re.search(r"from sqlalchemy import[^\n]*\b(insert|update|delete)\b", source), (
            path
        )
        assert ".execute(" not in source and "begin()" not in source, f"{path} runs SQL directly"
