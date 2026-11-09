"""Provider errors: classified, scrubbed, never carrying a key.

Classification (the call policy in app/ai/calls.py acts on `retryable`):
  retry:    429 (respect Retry-After), 5xx, timeouts, connection errors
  no retry: 400 bad request, 401/403 auth, content-policy refusals, malformed provider output

Provider error text is untrusted: keys can appear in it (some providers echo the Authorization
header or a key prefix). `scrub()` removes anything key-shaped and the exact secrets we know, and
truncates, BEFORE the text is stored or shown. Raw provider bodies are never stored at all; only a
short scrubbed excerpt is kept in memory for the safe `detail`.
"""

from __future__ import annotations

import re

# Safe, stable codes (lowercase snake_case, like the rest of the API's error envelope).
RATE_LIMITED = "provider_rate_limited"
UNAVAILABLE = "provider_unavailable"
TIMEOUT = "provider_timeout"
BAD_REQUEST = "provider_bad_request"
KEY_REJECTED = "key_rejected"
KEY_UNAVAILABLE = "key_unavailable"
REFUSED = "provider_refused"
BAD_OUTPUT = "provider_bad_output"
INPUT_TOO_LARGE = "input_too_large"
INCOMPLETE = "provider_output_truncated"
CANCELLED = "cancelled"

# Anything that looks like a credential: sk-..., sk-proj-..., AIza..., Bearer tokens, long hex/base64.
_KEY_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|[A-Za-z0-9_\-]{32,})"
)


def scrub(text: str, secrets: tuple[str, ...] = (), limit: int = 200) -> str:
    """Remove known secrets and credential-shaped tokens, collapse whitespace, truncate."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "[redacted]")
    out = _KEY_SHAPES.sub("[redacted]", out)
    out = " ".join(out.split())
    return out[:limit]


class ProviderError(Exception):
    """A classified provider failure. `str(error)` is the SAFE message (already scrubbed)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def classify_status(
    status: int, body_text: str, retry_after: float | None, secrets: tuple[str, ...]
) -> ProviderError:
    safe = scrub(body_text, secrets)
    if status == 429:
        return ProviderError(
            RATE_LIMITED, safe, status=status, retryable=True, retry_after=retry_after
        )
    if status >= 500:
        return ProviderError(
            UNAVAILABLE, safe, status=status, retryable=True, retry_after=retry_after
        )
    if status in (401, 403):
        return ProviderError(KEY_REJECTED, safe, status=status)
    if status == 400 and ("API_KEY_INVALID" in body_text or "API key not valid" in body_text):
        # Google reports an invalid key as 400, not 401 (Gemini API errors). It is a rejected key
        # wherever it happens, not only at save time, or a job would fail as a 'bad request' and
        # the user would never be told to replace the key.
        return ProviderError(KEY_REJECTED, safe, status=status)
    if status == 408:
        return ProviderError(TIMEOUT, safe, status=status, retryable=True)
    return ProviderError(BAD_REQUEST, safe, status=status)
