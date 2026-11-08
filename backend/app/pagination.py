"""Keyset (cursor) pagination helpers.

A cursor is the opaque base64url encoding of the last row's sort key: (timestamp, uuid) with
microsecond precision, which is exactly PostgreSQL's timestamptz precision, so no row is skipped or
repeated when many rows share a timestamp (the uuid breaks ties). Parsing is strict: anything that
does not re-encode to the identical string is rejected as invalid_cursor.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.errors import invalid_cursor

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
_PAYLOAD_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00)\|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12})"
)

T = TypeVar("T")


class Page(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic models use Generic[T]
    items: list[T]
    next_cursor: str | None


def encode_cursor(timestamp: datetime, key: uuid.UUID) -> str:
    stamp = timestamp.astimezone(UTC).isoformat(timespec="microseconds")
    return base64.urlsafe_b64encode(f"{stamp}|{key}".encode("ascii")).rstrip(b"=").decode("ascii")


def decode_cursor(raw: str | None) -> tuple[datetime, uuid.UUID] | None:
    if raw is None:
        return None
    try:
        if not _CURSOR_RE.fullmatch(raw):
            raise ValueError
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != raw:
            raise ValueError  # non-canonical encoding
        match = _PAYLOAD_RE.fullmatch(decoded.decode("ascii"))
        if match is None:
            raise ValueError
        return datetime.fromisoformat(match.group(1)), uuid.UUID(match.group(2))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise invalid_cursor() from None
