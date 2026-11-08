import logging

import httpx2
import pytest

from app.main import create_app


async def test_request_id_generated_on_success(client):
    response = await client.get("/health/live")
    assert len(response.headers["x-request-id"]) >= 8


async def test_request_id_honours_sane_inbound_value(client):
    response = await client.get("/health/live", headers={"X-Request-ID": "trace-abc-12345"})
    assert response.headers["x-request-id"] == "trace-abc-12345"


@pytest.mark.parametrize("bad", ["x", "has space and <script>", "a" * 200])
async def test_request_id_replaces_suspicious_inbound_value(client, bad):
    response = await client.get("/health/live", headers={"X-Request-ID": bad})
    assert response.headers["x-request-id"] != bad
    assert len(response.headers["x-request-id"]) >= 8


async def test_error_body_has_one_shape_and_matching_request_id(client):
    response = await client.get("/nope")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["request_id"] == response.headers["x-request-id"]


async def test_method_not_allowed_uses_error_shape(client):
    response = await client.post("/health/live")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_unhandled_exception_is_safe_and_carries_request_id(settings, engine):
    app = create_app(settings, engine=engine)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail SELECT * FROM users")

    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


async def test_access_log_has_request_id_and_never_the_query_string(client, caplog):
    caplog.set_level(logging.INFO, logger="threadforge")
    await client.get("/health/live?code=SUPERSECRETCODE&state=SUPERSECRETSTATE")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "/health/live" in messages
    assert "SUPERSECRET" not in messages
    assert any(getattr(r, "request_id", "-") != "-" for r in caplog.records)
