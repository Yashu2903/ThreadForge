"""Message request/response schemas.

Requests are strict: unknown fields are rejected, types are not coerced (a JSON string is required
for body, a JSON integer for expected_version), and no author, timestamp, role, room or version
field is accepted on creation. NO normalization of any kind is applied to the body: the stored text
is exactly what was validated and submitted (leading/trailing whitespace and line breaks included).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_BODY_CHARS = 5000
MAX_BIGINT = 2**63 - 1  # an expected_version above this would make PostgreSQL error (a 500)
_ALLOWED_CONTROL = {"\t", "\n", "\r"}
_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def validate_message_body(value: str) -> str:
    """Return `value` unchanged if acceptable, else raise ValueError (-> 422, never echoed).

    Length is counted in Unicode code points (1..5000). Rejected: NUL, lone surrogates (category
    Cs) and control characters (Cc) other than tab, LF and CR. NUL and lone surrogates would
    otherwise fail inside PostgreSQL or the UTF-8 encoder and surface as a 500. A body must also
    contain at least one VISIBLE character: not whitespace (str.isspace) and not a format character
    (category Cf: zero-width space/joiner, bidi marks, ...).
    """
    if not 1 <= len(value) <= MAX_BODY_CHARS:
        raise ValueError("body length out of range")
    visible = False
    for char in value:
        category = unicodedata.category(char)
        if char == "\x00" or category == "Cs":
            raise ValueError("body contains a forbidden character")
        if category == "Cc" and char not in _ALLOWED_CONTROL:
            raise ValueError("body contains a forbidden control character")
        if not char.isspace() and category != "Cf":
            visible = True
    if not visible:
        raise ValueError("body has no visible content")
    return value


def _parse_client_message_id(value: object) -> uuid.UUID:
    if not isinstance(value, str) or not _UUID_TEXT.fullmatch(value):
        raise ValueError("client_message_id must be a UUID string")
    return uuid.UUID(value)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SendMessageRequest(_StrictRequest):
    client_message_id: uuid.UUID
    body: str

    @field_validator("client_message_id", mode="before")
    @classmethod
    def _id(cls, value: object) -> uuid.UUID:
        return _parse_client_message_id(value)

    _body = field_validator("body")(validate_message_body)


class EditMessageRequest(_StrictRequest):
    body: str
    expected_version: int = Field(ge=1, le=MAX_BIGINT)

    _body = field_validator("body")(validate_message_body)


class MessageOut(BaseModel):
    """The one representation used everywhere. Tombstones have body null; deleted text is never
    returned. No fingerprint, email, provider identity or session data."""

    id: uuid.UUID
    room_id: uuid.UUID
    author_id: uuid.UUID
    author_display_name: str
    client_message_id: uuid.UUID
    body: str | None
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    version: int
    is_deleted: bool


class MessagePage(BaseModel):
    """History page: items oldest-to-newest; next_before is the cursor for the next OLDER page."""

    items: list[MessageOut]
    next_before: str | None
