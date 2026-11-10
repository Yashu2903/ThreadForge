"""One JSON error shape for every API route.

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Messages are fixed strings chosen by us. Exception text, SQL, secrets and provider response
bodies never reach a client.
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_config import APP_LOGGER
from app.middleware import no_store_path

log = logging.getLogger(f"{APP_LOGGER}.errors")

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
}


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
        # Extra machine-readable fields inside the error object (e.g. current_version).
        self.extra = extra or {}


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    request_id = request_id_of(request)
    response = JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id, **(extra or {})}},
        status_code=status_code,
        headers=headers,
    )
    # Set here as well as in the middleware: unhandled-exception responses are produced by
    # Starlette's outermost layer and would otherwise miss these headers.
    response.headers["X-Request-ID"] = request_id
    if no_store_path(request.url.path):
        response.headers["Cache-Control"] = "no-store"
    return response


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(
            request, exc.status_code, exc.code, exc.message, exc.headers, exc.extra
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = {404: "Not found.", 405: "Method not allowed."}.get(
            exc.status_code, "Request failed."
        )
        return error_response(request, exc.status_code, code, message, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's detail can echo submitted values, so none of it is returned.
        return error_response(request, 422, "validation_error", "The request was not valid.")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the type and location only. Exception text can carry SQL, parameters or provider
        # data, and logs must never contain secrets.
        frames = traceback.extract_tb(exc.__traceback__)
        where = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else "unknown"
        log.error("unhandled %s at %s (path %s)", type(exc).__name__, where, request.url.path)
        return error_response(request, 500, "internal_error", "Something went wrong.")


# ---- Part 2 error constructors (stable lowercase codes; documented in the README) -------------


def room_not_found() -> AppError:
    # Also used when the caller is simply not an active member: private rooms are never revealed.
    return AppError(404, "room_not_found", "Room not found.")


def insufficient_role() -> AppError:
    return AppError(403, "insufficient_role", "Your role does not allow this action.")


def member_not_found() -> AppError:
    return AppError(404, "member_not_found", "Member not found.")


def invitation_not_found() -> AppError:
    return AppError(404, "invitation_not_found", "Invitation not found.")


def invitation_invalid() -> AppError:
    # One status, one code, one message for EVERY redemption failure (unknown, malformed,
    # expired, revoked, used by someone else), so the response reveals nothing about why.
    return AppError(404, "invitation_invalid", "This invitation is not valid.")


def invitation_already_redeemed() -> AppError:
    return AppError(409, "invitation_already_redeemed", "This invitation was already redeemed.")


def owner_protected() -> AppError:
    return AppError(409, "owner_protected", "The room owner cannot be removed or demoted.")


def limit_reached(code: str, message: str) -> AppError:
    return AppError(409, code, message)


def invalid_cursor() -> AppError:
    return AppError(422, "invalid_cursor", "The pagination cursor is not valid.")


def validation_failed(message: str) -> AppError:
    return AppError(422, "validation_error", message)


# ---- Part 3 error constructors ----------------------------------------------------------------


def message_not_found() -> AppError:
    return AppError(404, "message_not_found", "Message not found.")


def message_not_author() -> AppError:
    return AppError(403, "message_not_author", "You cannot modify this message.")


def message_id_reused() -> AppError:
    return AppError(
        409,
        "message_id_reused",
        "This client_message_id was already used for a different message.",
    )


def message_version_conflict(current_version: int) -> AppError:
    return AppError(
        409,
        "message_version_conflict",
        "The message changed since you loaded it.",
        extra={"current_version": current_version},
    )


def message_deleted() -> AppError:
    return AppError(409, "message_deleted", "This message was deleted and cannot be edited.")


def rate_limited(
    retry_after: int, message: str = "Too many message writes. Try again shortly."
) -> AppError:
    return AppError(429, "rate_limited", message, headers={"Retry-After": str(retry_after)})


def payload_too_large() -> AppError:
    return AppError(413, "payload_too_large", "The request body is too large.")


# ---- Part 6 error constructors (documents) ------------------------------------------------------


def document_not_found() -> AppError:
    return AppError(404, "document_not_found", "Document not found.")


def document_request_id_reused() -> AppError:
    return AppError(
        409,
        "document_request_id_reused",
        "This client_request_id was already used for a different document.",
    )


def document_limit_reached() -> AppError:
    return AppError(
        409, "document_limit_reached", "This room has reached its maximum number of documents."
    )


def selection_conflict(current_selection_version: int) -> AppError:
    return AppError(
        409,
        "selection_conflict",
        "The active document changed since you loaded it.",
        extra={"current_selection_version": current_selection_version},
    )


def invalid_document_content(errors: list[dict[str, str]]) -> AppError:
    """422 with machine-readable field paths. Messages are fixed strings chosen by the validator
    (they never echo document text), so they are safe to return."""
    return AppError(
        422,
        "invalid_document_content",
        "The document content is not valid.",
        extra={"errors": errors},
    )


def asset_not_found(asset_ids: list[str] | None = None) -> AppError:
    extra = {"asset_ids": asset_ids[:20]} if asset_ids else None
    return AppError(422 if asset_ids else 404, "asset_not_found", "Asset not found.", extra=extra)


# ---- Part 6: assets ----------------------------------------------------------------------


def unsupported_media_type(
    message: str = "Only PNG, JPEG and WebP images are accepted.",
) -> AppError:
    return AppError(415, "unsupported_media_type", message)


def invalid_image(reason: str, message: str) -> AppError:
    return AppError(422, "invalid_image", message, extra={"reason": reason})


def quota_exceeded(message: str) -> AppError:
    return AppError(409, "quota_exceeded", message)


# ---- ThreadForge AI (phase 1) -------------------------------------------------------------------


def ai_not_configured() -> AppError:
    return AppError(503, "ai_not_configured", "AI is not configured on this server.")


def ai_disabled() -> AppError:
    return AppError(503, "ai_disabled", "AI has been switched off by the server operator.")


def ai_unavailable(detail_code: str = "") -> AppError:
    extra = {"capability_code": detail_code} if detail_code else None
    return AppError(
        503,
        "ai_unavailable",
        "AI is temporarily unavailable: a provider check is failing.",
        extra=extra,
    )


def ai_disabled_for_room() -> AppError:
    return AppError(409, "ai_disabled_for_room", "The room owner has switched AI off in this room.")


def consent_required() -> AppError:
    return AppError(403, "consent_required", "Read and accept the AI data notice first.")


def ai_write_access_required() -> AppError:
    return AppError(
        403,
        "ai_write_access_required",
        "AI requests need write access to the document you are working in.",
    )


def ai_anchor_required() -> AppError:
    return AppError(
        409,
        "ai_anchor_required",
        "Open or create a document you can edit first; AI requests are tied to one.",
    )


def key_unavailable(provider: str) -> AppError:
    return AppError(
        409,
        "key_unavailable",
        "No usable API key is available for this provider.",
        extra={"provider": provider},
    )


def key_rejected(provider: str | None = None) -> AppError:
    extra = {"provider": provider} if provider else None
    return AppError(422, "key_rejected", "The provider rejected this API key.", extra=extra)


def budget_exhausted(scope: str) -> AppError:
    return AppError(
        429,
        "budget_exhausted",
        "The AI spending budget for this period is used up.",
        extra={"scope": scope},
    )


# ---- ThreadForge AI: sources (checkpoint 2) ------------------------------------------------------


def source_not_found() -> AppError:
    return AppError(404, "source_not_found", "Source not found.")


def source_upload_not_found() -> AppError:
    return AppError(404, "source_upload_not_found", "Upload not found.")


def file_type_error(code: str, message: str) -> AppError:
    """415 with the specific code: unsupported_media_type (not an accepted kind) or type_mismatch
    (the name, the declared type or the real bytes disagree)."""
    return AppError(415, code, message)


def invalid_source(reason: str, message: str) -> AppError:
    return AppError(422, "invalid_source", message, extra={"reason": reason})


def source_upload_not_open(state: str) -> AppError:
    return AppError(
        409,
        "source_upload_not_open",
        "This upload cannot accept that step in its current state.",
        extra={"state": state},
    )


def source_upload_id_reused() -> AppError:
    return AppError(
        409,
        "source_upload_id_reused",
        "That client_request_id was already used for a different upload.",
    )


def source_unavailable(version_ids: list[str]) -> AppError:
    return AppError(
        409,
        "source_unavailable",
        "A selected file was removed or does not belong to this room.",
        extra={"source_version_ids": version_ids},
    )


# ---- ThreadForge AI: analysis (checkpoint 3) ----------------------------------------------


def analysis_cap_exceeded(cap: str, limit: int, actual: int) -> AppError:
    return AppError(
        409,
        "analysis_cap_exceeded",
        "This file is larger than one request may analyze.",  # noqa: E501
        extra={"cap": cap, "limit": limit, "actual": actual},
    )


def source_version_not_found() -> AppError:
    return AppError(404, "source_version_not_found", "File version not found.")


# ---- ThreadForge AI: requests (checkpoint 4) -------------------------------------


def message_locked() -> AppError:
    return AppError(409, "message_locked", "AI requests and AI answers cannot be edited.")


def ai_request_not_found() -> AppError:
    return AppError(404, "ai_request_not_found", "AI request not found.")


def ai_request_id_reused() -> AppError:
    return AppError(
        409,
        "ai_request_id_reused",
        "That client_request_id was already used for a different request.",
    )


def ai_context_required() -> AppError:
    return AppError(
        422,
        "ai_context_required",
        "Select at least one file or include the open document (chat alone is not enough).",
    )


def invalid_ai_request(reason: str, message: str) -> AppError:
    return AppError(422, "invalid_ai_request", message, extra={"reason": reason})


def edit_target_required() -> AppError:
    return AppError(
        422,
        "edit_target_required",
        "Select the blocks or the text to change, or choose where new text goes. "
        "The assistant never guesses where to write.",
    )


def ai_proposals_disabled() -> AppError:
    return AppError(
        503,
        "ai_proposals_disabled",
        "Edit proposals and drafts are not switched on on this server.",
    )


def proposal_rate_limited(limit: int) -> AppError:
    return AppError(
        429,
        "proposal_rate_limited",
        "You have asked for many proposals in the last hour. Try again a little later.",
        extra={"limit_per_hour": limit},
    )


def proposal_not_pending(state: str) -> AppError:
    messages = {
        "applied": "This proposal was already applied.",
        "rejected": "This proposal was rejected.",
        "conflicted": "The document changed since this proposal was made. Ask for a new one.",
        "expired": "This proposal expired. Ask for a new one.",
        "superseded": "A newer proposal replaced this one.",
        "applying": "This proposal is being applied right now.",
    }
    return AppError(
        409,
        f"proposal_{state}",
        messages.get(state, "This proposal can no longer be applied."),
        extra={"state": state},
    )


def ai_proposal_not_found() -> AppError:
    return AppError(404, "ai_proposal_not_found", "Proposal not found.")


def ai_capacity(scope: str, limit: int) -> AppError:
    return AppError(
        429,
        "ai_capacity",
        "Too many AI requests are already running. Wait for one to finish.",
        extra={"scope": scope, "limit": limit},
    )


def confirmation_required(estimate: dict) -> AppError:
    return AppError(
        409,
        "confirmation_required",
        "This request is large. Confirm to send it.",
        extra={"estimate": estimate},
    )


def ai_request_not_cancellable(state: str) -> AppError:
    return AppError(
        409,
        "ai_request_not_cancellable",
        "This request has already finished.",
        extra={"state": state},
    )


def ai_request_not_retryable(reason: str) -> AppError:
    return AppError(
        409, "ai_request_not_retryable", "This request cannot be retried.", extra={"reason": reason}
    )
