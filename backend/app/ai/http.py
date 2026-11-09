"""One HTTP client wrapper for both providers.

We use plain HTTP (httpx2) and not the vendor SDKs on purpose: the call policy needs exact control of
retries (the SDKs retry silently, which would hide spend from the ledger), timeouts, and the raw usage
numbers. The ONE place a provider key is placed on a request is `_auth_headers` in each adapter.
"""

from __future__ import annotations

import email.utils
import time
from typing import Any

import httpx2

from app.ai import errors


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either seconds or an HTTP date. Capped at 10 minutes."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = parsed.timestamp() - time.time()
    return max(0.0, min(seconds, 600.0))


class ProviderHttp:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(timeout, connect=min(10.0, timeout)),
            transport=transport,
            follow_redirects=False,  # a redirect could send the key to another host
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self._key,)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request. Returns parsed JSON or raises a classified, scrubbed ProviderError."""
        try:
            response = await self._client.request(
                method, f"{self._base}{path}", headers=headers, json=json
            )
        except httpx2.TimeoutException:
            raise errors.ProviderError(
                errors.TIMEOUT, "request timed out", retryable=True
            ) from None
        except httpx2.HTTPError as exc:
            # connection errors etc.: the exception text can contain the URL; use only the class name
            raise errors.ProviderError(
                errors.UNAVAILABLE, type(exc).__name__, retryable=True
            ) from None
        if response.status_code >= 400:
            raise errors.classify_status(
                response.status_code,
                response.text[:2000],
                parse_retry_after(response.headers.get("retry-after")),
                self.secrets,
            )
        try:
            body = response.json()
        except ValueError:
            raise errors.ProviderError(
                errors.BAD_OUTPUT, "response was not JSON", status=response.status_code
            ) from None
        if not isinstance(body, dict):
            raise errors.ProviderError(errors.BAD_OUTPUT, "unexpected response shape")
        return body
