"""Pure-ASGI middleware: request IDs, access logging, and no-store on auth routes.

Pure ASGI (not BaseHTTPMiddleware) so it adds no extra task layer and cannot swallow
exceptions or break streaming later when WebSockets arrive.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import APP_LOGGER, request_id_var

access_log = logging.getLogger(f"{APP_LOGGER}.access")


def no_store_path(path: str) -> bool:
    """Every response except health checks is private (session, rooms, invitation tokens): never
    let a browser or shared cache keep it."""
    return not path.startswith("/health/")


# Accept a caller-supplied ID (so a proxy's ID can be correlated) only if it is boring.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = MutableHeaders(scope=scope).get("x-request-id", "")
        request_id = supplied if _VALID_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        path: str = scope["path"]
        no_store = no_store_path(path)
        status = 500
        started = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if no_store:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            client = scope.get("client")
            # Path only, never the query string: /auth/callback carries the authorization code
            # and state in its query.
            access_log.info(
                "%s %s -> %s in %.1fms client=%s",
                scope["method"],
                path,
                status,
                (time.perf_counter() - started) * 1000,
                client[0] if client else "-",
            )
            request_id_var.reset(token)
