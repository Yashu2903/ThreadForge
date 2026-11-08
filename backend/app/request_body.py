"""Read and validate a JSON request body AFTER the authentication, CSRF and room gates.

FastAPI normally parses the body before it runs dependencies, so a malformed body would be
answered with 422 before the caller is even authenticated. Message routes instead declare no body
parameter, run their gates as dependencies, and then call parse_json_body(), so the order is
exactly: 401, 403 (csrf), 404/403 (room access), 422 (validation). Errors never echo input.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ValidationError

from app.errors import validation_failed


async def parse_json_body[M: BaseModel](request: Request, model: type[M]) -> M:
    raw = await request.body()  # already size-limited by BodySizeLimitMiddleware
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):  # includes UnicodeDecodeError
        raise validation_failed("The request was not valid.") from None
    try:
        return model.model_validate(data)
    except (ValidationError, ValueError):
        # Pydantic's error entries contain the submitted value; none of it is returned or logged.
        raise validation_failed("The request was not valid.") from None


def json_body_docs(model: type[BaseModel]) -> dict[str, Any]:
    """OpenAPI documentation for a body the route parses itself."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }
