"""Request schemas for the AI routes. Strict: unknown fields are rejected, so a client cannot supply
an actor, a role, a key source or a price."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SaveKeyRequest(_Strict):
    api_key: str = Field(
        min_length=1, max_length=400, repr=False
    )  # repr=False: never in a log line


class ConsentRequest(_Strict):
    notice_version: int


class RoomAiSettingsRequest(_Strict):
    ai_enabled: bool


class EstimateRequest(_Strict):
    instruction: str = Field(min_length=1, max_length=4000)
    task_hint: Literal["auto", "ask", "summarize", "compare"] = "auto"
    # strict=False: JSON carries ids as strings; every other field stays strictly typed
    document_id: uuid.UUID | None = Field(default=None, strict=False)
    include_document_content: bool = False
    document_scope: Literal["none", "whole", "blocks"] = "none"
    selected_chars: int = Field(default=0, ge=0, le=1_000_000)
    include_chat_context: bool = False
    chat_message_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20, strict=False)
