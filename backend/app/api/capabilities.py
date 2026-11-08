"""GET /capabilities: what this server can do, so the UI shows only controls that work.

It reports only `available` or `not_configured` per optional feature, never a secret, a key id, a
client id or any configuration value. Authentication is required (it is not a public probe).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.dependencies.auth import CurrentUser
from app.schemas.auth import ErrorBody

router = APIRouter(tags=["capabilities"])


class CapabilitiesOut(BaseModel):
    google_drive: Literal["available", "not_configured"]


@router.get(
    "/capabilities",
    response_model=CapabilitiesOut,
    responses={401: {"model": ErrorBody, "description": "unauthenticated"}},
)
async def capabilities(request: Request, _user: CurrentUser) -> CapabilitiesOut:
    settings = request.app.state.settings
    return CapabilitiesOut(
        google_drive="available" if settings.drive_configured else "not_configured"
    )
