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
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = request_id_of(request)
    response = JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id}},
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
        return error_response(request, exc.status_code, exc.code, exc.message, exc.headers)

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
