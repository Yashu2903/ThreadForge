"""Request-body size limit (413), as pure ASGI middleware.

Why 128 KiB: a message is at most 5,000 characters. Fully JSON-escaped, every character as a
6-byte unicode escape is 30 KB, and even an emoji-only body written as 12-byte surrogate-pair
escapes is at most about 60 KB. 128 KiB leaves generous headroom and still bounds memory.

How it enforces the limit:
  * A Content-Length larger than the limit is rejected immediately, before reading anything.
  * But the header is never trusted alone (it can be missing, as with chunked uploads, or false):
    the body is also counted as it streams in, and reading stops the moment the running total
    passes the limit.
Within the limit the (small) body is buffered and replayed to the application.

The middleware sits inside RequestContextMiddleware, so the 413 carries X-Request-ID, the
request id in the error body, and Cache-Control: no-store like every other response.
"""

from __future__ import annotations

import json
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 128 * 1024


class BodySizeLimitMiddleware:
    """`rules` are per-route overrides: (METHOD, compiled path regex, max_bytes). The first rule
    that matches wins; everything else gets `max_bytes`. (Part 6: a document save may be 2 MiB
    while every other JSON route stays at 128 KiB.)"""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_BODY_BYTES,
        rules: tuple[tuple[str, re.Pattern[str], int], ...] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.rules = rules

    def limit_for(self, scope: Scope) -> int:
        for method, pattern, limit in self.rules:
            if scope.get("method") == method and pattern.fullmatch(scope.get("path", "")):
                return limit
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope)
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await self._reject(scope, send)
            return

        # Read the body ourselves, counting bytes as they arrive.
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # e.g. http.disconnect: pass it on untouched
                await self.app(scope, _replay(chunks, message, receive), send)
                return
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                await self._reject(scope, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        await self.app(scope, _replay(chunks, None, receive), send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        request_id = scope.get("state", {}).get("request_id", "-")
        payload = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": "The request body is too large.",
                    "request_id": request_id,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                declared = int(value)
            except ValueError:
                return None
            return declared if declared >= 0 else None
    return None


def _replay(chunks: list[bytes], pending: Message | None, original: Receive) -> Receive:
    """Hand the buffered body to the app as one message, then defer to the real receive()."""
    state = {"sent_body": False, "pending": pending}

    async def receive() -> Message:
        if not state["sent_body"]:
            state["sent_body"] = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        if state["pending"] is not None:
            message, state["pending"] = state["pending"], None
            return message
        return await original()

    return receive
