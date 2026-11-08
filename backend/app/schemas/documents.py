"""Document request/response schemas.

Requests are strict (unknown fields rejected, no type coercion) and carry no actor, role, version
timestamp or room field the server should decide. The document CONTENT is validated separately by
app.documents.schema.validate_document (it needs paths and canonicalization, not just types).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.documents.schema import title_error

MAX_BIGINT = 2**63 - 1
_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _clean_title(value: str) -> str:
    trimmed = value.strip()
    problem = title_error(trimmed)
    if problem:
        raise ValueError(f"title {problem}")
    return trimmed


def _parse_uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str) or not _UUID_TEXT.fullmatch(value):
        raise ValueError("must be a UUID string")
    return uuid.UUID(value)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateDocumentRequest(_StrictRequest):
    title: str
    template_key: Literal[
        "blank", "project_brief", "technical_proposal", "meeting_notes", "research_summary"
    ]
    client_request_id: uuid.UUID

    _title = field_validator("title")(_clean_title)

    @field_validator("client_request_id", mode="before")
    @classmethod
    def _id(cls, value: object) -> uuid.UUID:
        return _parse_uuid(value)


class RenameDocumentRequest(_StrictRequest):
    title: str

    _title = field_validator("title")(_clean_title)


class SetActiveDocumentRequest(_StrictRequest):
    document_id: uuid.UUID
    expected_selection_version: int | None = Field(default=None, ge=0, le=MAX_BIGINT)

    @field_validator("document_id", mode="before")
    @classmethod
    def _id(cls, value: object) -> uuid.UUID:
        return _parse_uuid(value)


class SaveContentRequest(_StrictRequest):
    schema_version: int
    expected_content_version: int = Field(ge=1, le=MAX_BIGINT)
    content: dict[str, Any]


class DocumentSummaryOut(BaseModel):
    """What the list shows. Never includes content."""

    id: uuid.UUID
    room_id: uuid.UUID
    title: str
    template_key: str
    schema_version: int
    content_version: int
    metadata_version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    content_updated_at: datetime
    is_active: bool
    requires_review: (
        bool  # an import with review-level warnings (always false before imports exist)
    )


class DocumentOut(DocumentSummaryOut):
    content: dict[str, Any]
    content_hash: str  # hex SHA-256 of the canonical serialization
    can_edit: bool  # the CALLER's permission (owner or editor)


class SaveContentOut(BaseModel):
    content_version: int
    content_hash: str
    canonical_changed: bool
    unchanged: bool
    # present only when canonical_changed: the server's canonical form of what you sent
    content: dict[str, Any] | None = None


class ActiveDocumentOut(BaseModel):
    active_document_id: uuid.UUID | None
    active_document_version: int
