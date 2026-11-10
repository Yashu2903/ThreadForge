"""/internal/collab/*: the API the Node collaboration service (collab/) uses. Never the browser's.

Gate order:
  1. network: the reverse proxy does not route /internal, and the service is on a private network;
  2. shared secret: the X-Internal-Secret header, compared in constant time; if INTERNAL_API_SECRET
     is not configured every route answers 503 (the feature is off, not open);
  3. for writes, the lease token (X-Collab-Lease) is checked inside the write transaction
     (services/collab.py: fenced writes).

No cookies, no CSRF (a browser can neither read nor send the secret), no CORS headers (this app
installs no CORS middleware; nothing here is ever called cross-origin). The secret, cookies and
document content are never logged.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.errors import AppError
from app.services import ai_proposals as proposals
from app.services import collab
from app.services.permissions import decide_write

SECRET_HEADER = "X-Internal-Secret"  # noqa: S105 (a header NAME, not a secret)
LEASE_HEADER = "X-Collab-Lease"
COUNTER_HEADER = "X-Update-Counter"


def require_internal_secret(
    request: Request, x_internal_secret: Annotated[str | None, Header()] = None
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.internal_api_secret.get_secret_value()
    if not expected:
        raise AppError(503, "internal_api_disabled", "The internal API is not configured.")
    # compare_digest on bytes: constant time, and it accepts non-ASCII input without raising.
    if x_internal_secret is None or not hmac.compare_digest(
        x_internal_secret.encode(), expected.encode()
    ):
        raise AppError(401, "unauthenticated", "Invalid internal credentials.")


router = APIRouter(
    prefix="/internal/collab",
    tags=["internal-collab"],
    dependencies=[Depends(require_internal_secret)],
    include_in_schema=False,
)


def _lease_token(x_collab_lease: Annotated[str | None, Header()] = None) -> int:
    try:
        return int(x_collab_lease or "")
    except ValueError:
        raise AppError(422, "validation_error", "X-Collab-Lease must be an integer.") from None


LeaseToken = Annotated[int, Depends(_lease_token)]


def _document_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError(404, "document_not_found", "Document not found.") from None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeaseIn(_Body):
    holder_id: str = Field(min_length=8, max_length=128)


class LeaseRenewIn(LeaseIn):
    token: int = Field(ge=1)


class LeaseOut(BaseModel):
    token: int
    expires_at: str
    ttl_seconds: int


class AuthorizeIn(_Body):
    cookie_header: str | None = Field(default=None, max_length=8192)
    document_id: uuid.UUID


class AuthorizeOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    session_id: uuid.UUID
    role: str
    can_edit: bool
    room_id: uuid.UUID
    document_id: uuid.UUID
    collab_state: str


class SeedOut(BaseModel):
    format: str
    content: dict[str, Any]
    room_id: uuid.UUID


class MarkIn(_Body):
    state: str
    reason: str = Field(min_length=1, max_length=64)


class SnapshotIn(_Body):
    kind: str
    yjs_update_counter: int = Field(ge=0)
    content: dict[str, Any]
    content_hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    text_length: int = Field(ge=0)


class SnapshotOut(BaseModel):
    stored: bool
    snapshot_id: uuid.UUID | None


class AccessConnectionIn(_Body):
    session_id: uuid.UUID
    document_id: uuid.UUID


class AccessRoomIn(_Body):
    room_id: uuid.UUID
    connections: list[AccessConnectionIn] = Field(max_length=2000)


class AccessCheckIn(_Body):
    rooms: list[AccessRoomIn] = Field(max_length=200)


class AccessResultOut(BaseModel):
    status: str
    can_edit: bool


class AccessRoomOut(BaseModel):
    room_id: uuid.UUID
    results: list[AccessResultOut]


class AccessCheckOut(BaseModel):
    rooms: list[AccessRoomOut]


class CanWriteIn(_Body):
    user_id: uuid.UUID
    document_id: uuid.UUID


class CanWriteOut(BaseModel):
    allowed: bool
    reason: str
    role_ok: bool
    role: str | None
    collab_state: str | None
    room_id: uuid.UUID | None


class AssetCheckIn(_Body):
    room_id: uuid.UUID
    asset_ids: list[uuid.UUID] = Field(max_length=500)


class AssetCheckOut(BaseModel):
    missing: list[str]


def _lease_out(request: Request, lease: collab.Lease) -> LeaseOut:
    return LeaseOut(
        token=lease.token,
        expires_at=lease.expires_at.isoformat(),
        ttl_seconds=request.app.state.settings.collab_lease_ttl_seconds,
    )


@router.post("/lease/acquire", response_model=LeaseOut)
async def acquire(request: Request, body: LeaseIn) -> LeaseOut:
    lease = await collab.acquire_lease(
        request.app.state.sessionmaker, request.app.state.settings, body.holder_id
    )
    return _lease_out(request, lease)


@router.post("/lease/renew", response_model=LeaseOut)
async def renew(request: Request, body: LeaseRenewIn) -> LeaseOut:
    lease = await collab.renew_lease(
        request.app.state.sessionmaker, request.app.state.settings, body.holder_id, body.token
    )
    return _lease_out(request, lease)


@router.post("/lease/release", status_code=204)
async def release(request: Request, body: LeaseRenewIn) -> Response:
    await collab.release_lease(request.app.state.sessionmaker, body.holder_id, body.token)
    return Response(status_code=204)


@router.post("/authorize", response_model=AuthorizeOut)
async def authorize(request: Request, body: AuthorizeIn) -> AuthorizeOut:
    access = await collab.authorize_document(
        request.app.state.sessionmaker,
        request.app.state.settings,
        body.cookie_header,
        body.document_id,
    )
    return AuthorizeOut(**access.__dict__)


@router.get("/documents/{document_id}/state")
async def get_state(request: Request, document_id: str) -> Response:
    """Octet-stream body = the Yjs state (empty if never seeded). Metadata in headers so the body
    stays raw bytes: X-Collab-State, X-Update-Counter (-1 = no row), X-Room-Id."""
    stored = await collab.load_state(request.app.state.sessionmaker, _document_uuid(document_id))
    if stored is None:
        raise AppError(404, "document_not_found", "Document not found.")
    return Response(
        content=stored.state,
        media_type="application/octet-stream",
        headers={
            "X-Collab-State": stored.collab_state,
            COUNTER_HEADER: str(stored.update_counter),
            "X-Room-Id": str(stored.room_id),
        },
    )


@router.put("/documents/{document_id}/state", status_code=204)
async def put_state(
    request: Request,
    document_id: str,
    token: LeaseToken,
    x_update_counter: Annotated[int, Header(ge=0)],
    x_insert_if_absent: Annotated[bool, Header()] = False,
    x_proposal_id: Annotated[str | None, Header()] = None,
    x_applied_by: Annotated[str | None, Header()] = None,
    x_receipt_hash: Annotated[str | None, Header()] = None,
) -> Response:
    state = await request.body()
    receipt = None
    if x_proposal_id or x_applied_by or x_receipt_hash:
        try:
            receipt = collab.ApplyReceipt(
                uuid.UUID(x_proposal_id or ""),
                uuid.UUID(x_applied_by or ""),
                bytes.fromhex(x_receipt_hash or ""),
            )
            if len(receipt.receipt_hash) != 32:
                raise ValueError("hash length")
        except ValueError:
            raise AppError(
                422, "validation_error", "The application receipt is malformed."
            ) from None
    await collab.store_state(
        request.app.state.sessionmaker,
        request.app.state.settings,
        token,
        _document_uuid(document_id),
        x_update_counter,
        state,
        x_insert_if_absent,
        receipt,
    )
    return Response(status_code=204)


# ---- AI proposals being applied (phase 2) ----------------------------------------


class BeginApplyIn(_Body):
    user_id: uuid.UUID
    document_id: uuid.UUID


class FinishApplyIn(_Body):
    user_id: uuid.UUID
    resulting_counter: int = Field(ge=0)
    receipt_hash: str = Field(min_length=64, max_length=64)


class ReleaseApplyIn(_Body):
    reason: str = Field(min_length=1, max_length=60)


class ConflictedIn(_Body):
    user_id: uuid.UUID
    code: str = Field(min_length=1, max_length=48)


def _proposal_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError(404, "ai_proposal_not_found", "Proposal not found.") from None


@router.get("/proposals/stuck")
async def stuck_proposals(request: Request, older_than_seconds: int = 60) -> dict[str, Any]:
    """The recovery sweeper's work list: proposals that have been `applying` for a while."""
    rows = await proposals.stuck_proposals(
        request.app.state.sessionmaker, max(0, min(older_than_seconds, 86_400))
    )
    return {"proposals": rows}


@router.post("/proposals/{proposal_id}/begin-apply")
async def begin_apply(
    request: Request, proposal_id: str, body: BeginApplyIn, token: LeaseToken
) -> dict[str, Any]:
    """Recheck the user's write access and the proposal, and move it pending -> applying."""
    return await proposals.begin_apply(
        request.app.state.sessionmaker,
        request.app.state.settings,
        token,
        proposal_id=_proposal_uuid(proposal_id),
        document_id=body.document_id,
        user_id=body.user_id,
    )


@router.post("/proposals/{proposal_id}/finish-apply")
async def finish_apply(
    request: Request, proposal_id: str, body: FinishApplyIn, token: LeaseToken
) -> dict[str, str]:
    try:
        receipt = bytes.fromhex(body.receipt_hash)
    except ValueError:
        raise AppError(422, "validation_error", "The receipt hash is malformed.") from None
    state = await proposals.finish_apply(
        request.app.state.sessionmaker,
        token,
        _proposal_uuid(proposal_id),
        body.user_id,
        body.resulting_counter,
        receipt,
    )
    return {"state": state}


@router.post("/proposals/{proposal_id}/release-apply", status_code=204)
async def release_apply(
    request: Request, proposal_id: str, body: ReleaseApplyIn, token: LeaseToken
) -> Response:
    await proposals.release_apply(
        request.app.state.sessionmaker, token, _proposal_uuid(proposal_id), body.reason
    )
    return Response(status_code=204)


@router.post("/proposals/{proposal_id}/conflicted", status_code=204)
async def conflicted(
    request: Request, proposal_id: str, body: ConflictedIn, token: LeaseToken
) -> Response:
    await proposals.mark_conflicted(
        request.app.state.sessionmaker, token, _proposal_uuid(proposal_id), body.user_id, body.code
    )
    return Response(status_code=204)


@router.get("/documents/{document_id}/seed", response_model=SeedOut)
async def get_seed(request: Request, document_id: str) -> SeedOut:
    seed = await collab.get_seed(request.app.state.sessionmaker, _document_uuid(document_id))
    return SeedOut(format=seed.format, content=seed.content, room_id=seed.room_id)


@router.post("/documents/{document_id}/mark", status_code=204)
async def mark(request: Request, document_id: str, body: MarkIn, token: LeaseToken) -> Response:
    await collab.mark_state(
        request.app.state.sessionmaker, token, _document_uuid(document_id), body.state, body.reason
    )
    return Response(status_code=204)


@router.post("/documents/{document_id}/snapshot", response_model=SnapshotOut)
async def snapshot(
    request: Request, document_id: str, body: SnapshotIn, token: LeaseToken
) -> SnapshotOut:
    result = await collab.write_snapshot(
        request.app.state.sessionmaker,
        request.app.state.settings,
        token,
        _document_uuid(document_id),
        body.kind,
        body.yjs_update_counter,
        body.content,
        bytes.fromhex(body.content_hash),
        body.text_length,
    )
    return SnapshotOut(stored=result.stored, snapshot_id=result.snapshot_id)


@router.post("/assets/check", response_model=AssetCheckOut)
async def assets_check(request: Request, body: AssetCheckIn) -> AssetCheckOut:
    missing = await collab.check_assets(
        request.app.state.sessionmaker, body.room_id, body.asset_ids
    )
    return AssetCheckOut(missing=missing)


@router.post("/access/check", response_model=AccessCheckOut)
async def access_check(request: Request, body: AccessCheckIn) -> AccessCheckOut:
    """Batched re-check of OPEN connections, one pair of queries per room (see
    services/collab.check_access_batch). Read-only: no lease needed, nothing is written."""
    sessionmaker = request.app.state.sessionmaker
    rooms: list[AccessRoomOut] = []
    for room in body.rooms:
        answers = await collab.check_access_batch(
            sessionmaker, room.room_id, [(c.session_id, c.document_id) for c in room.connections]
        )
        rooms.append(
            AccessRoomOut(
                room_id=room.room_id,
                results=[AccessResultOut(status=a.status, can_edit=a.can_edit) for a in answers],
            )
        )
    return AccessCheckOut(rooms=rooms)


@router.post("/can-write", response_model=CanWriteOut)
async def can_write(request: Request, body: CanWriteIn) -> CanWriteOut:
    """May this USER (not a cookie) write to this document right now? The same decide_write the
    browser-facing authorize uses, for server-side callers that act on a person's behalf (the AI
    hooks): the requester is re-checked on every call, never trusted from an earlier one."""
    async with request.app.state.sessionmaker() as db:
        decision = await decide_write(
            db, request.app.state.settings, body.user_id, body.document_id
        )
    return CanWriteOut(
        allowed=decision.allowed,
        reason=decision.reason,
        role_ok=decision.role_ok,
        role=decision.role,
        collab_state=decision.collab_state,
        room_id=decision.room_id,
    )
