import uuid

from pydantic import BaseModel


class UserOut(BaseModel):
    """The only user fields the API exposes: nothing about state, version or identities."""

    id: uuid.UUID
    display_name: str
    email: str | None
    email_verified: bool


class CsrfOut(BaseModel):
    csrf_token: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorBody(BaseModel):
    error: ErrorDetail
