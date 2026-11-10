"""The API's one call to the collaboration service for proposals: `POST /internal/ai/apply-proposal`.

The service changes the shared document (docs/ai.md, "Applying an edit proposal"); this module only carries the
request over the private network with the shared secret and turns the answer into the API's own error codes.
Nothing here ever sees document content: the reply carries a state, two booleans and counters.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx2

from app.config import Settings
from app.errors import AppError


async def apply_proposal(
    settings: Settings,
    *,
    proposal_id: uuid.UUID,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> tuple[int, dict[str, Any]]:
    secret = settings.internal_api_secret.get_secret_value()
    if not secret:
        return 503, {"error": "unavailable"}
    # the service may wait up to AI_APPLY_MAX_WAIT_SECONDS for the document to be quiet
    timeout = settings.ai_collab_timeout_seconds + settings.ai_apply_max_wait_seconds + 10
    try:
        async with httpx2.AsyncClient(
            base_url=settings.collab_service_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/internal/ai/apply-proposal",
                headers={"X-Internal-Secret": secret},
                json={
                    "proposal_id": str(proposal_id),
                    "document_id": str(document_id),
                    "user_id": str(user_id),
                },
            )
    except (httpx2.TimeoutException, httpx2.TransportError):
        return 503, {"error": "unavailable"}
    try:
        body = response.json()
    except ValueError:
        return 503, {"error": "unavailable"}
    return response.status_code, body if isinstance(body, dict) else {}


def error_for(status: int, body: dict[str, Any]) -> AppError:
    """The collaboration service's refusal as one of the API's errors."""
    code = str(body.get("error", ""))
    if code == "conflicted":
        return AppError(
            409,
            "proposal_conflicted",
            "The document changed since this was proposed, so nothing was changed. "
            "Ask for a new proposal from the current text.",
            extra={"reason": str(body.get("code", "block_changed"))},
        )
    if code == "document_busy":
        return AppError(
            409,
            "document_busy",
            "Someone is editing the document right now. Try again in a few seconds.",
            extra={"retryable": True},
        )
    if status == 409 and code.startswith("proposal_"):
        return AppError(409, code, "This proposal can no longer be applied.", extra=_state(body))
    if status == 403 or code in ("forbidden", "ai_write_access_required"):
        return AppError(
            403, "ai_write_access_required", "You no longer have write access to this document."
        )
    if status == 404 or code in ("ai_proposal_not_found", "document_not_found"):
        return AppError(404, "ai_proposal_not_found", "Proposal not found.")
    return AppError(
        503, "collab_unavailable", "The editing service could not be reached. Nothing was changed."
    )


def _state(body: dict[str, Any]) -> dict[str, Any]:
    return {"state": body["state"]} if "state" in body else {}
