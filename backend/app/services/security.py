"""Small primitives for random secrets and their stored hashes."""

from __future__ import annotations

import hashlib
import re
import secrets

# token_urlsafe(32) produces 43 URL-safe characters from 32 random bytes (256 bits).
SECRET_BYTES = 32
# Accepted shape of any secret read back from a cookie or query string. Rejecting everything else
# early means malformed input never reaches a hash function, a query, or a log line.
SECRET_FORMAT = re.compile(r"[A-Za-z0-9_-]{43,128}")


def new_secret(nbytes: int = SECRET_BYTES) -> str:
    """A cryptographically secure random secret, safe for cookies and URLs."""
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    """Hash for storage. These secrets already have 256 bits of entropy, so a fast unsalted hash
    is the right tool (a slow password hash would only add cost without adding security)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_well_formed_secret(value: str | None) -> bool:
    return bool(value) and SECRET_FORMAT.fullmatch(value) is not None
