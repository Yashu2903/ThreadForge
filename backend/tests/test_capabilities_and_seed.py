"""GET /capabilities and the test-only session seeding script."""

import pytest

from scripts import seed_session
from tests.factories import (
    TEST_CSRF_SECRET,
    TEST_DATABASE_URL,
    TEST_FINGERPRINT_SECRET,
    make_production_settings,
    make_settings,
)
from tests.helpers import put_cookie, rows, scalar
from tests.test_invitations_api import new_room

DRIVE = {
    "google_drive_client_id": "1234-abc.apps.googleusercontent.com",
    "google_drive_client_secret": "drive-secret-value-0123456789",
    "google_picker_api_key": "AIzaSyExampleKeyForTestsOnly0123456789",
    "google_cloud_project_number": "123456789012",
    "cloud_token_encryption_keys": "k1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}


async def test_capabilities_requires_authentication(client):
    response = await client.get("/capabilities")
    assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthenticated")


async def test_drive_is_not_configured_by_default(alice):
    response = await alice.get("/capabilities")
    assert response.status_code == 200
    assert response.json() == {"google_drive": "not_configured"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("missing", list(DRIVE))
async def test_drive_needs_every_setting(make_actor, missing):
    cfg = make_settings(**{k: v for k, v in DRIVE.items() if k != missing})
    assert cfg.drive_configured is False
    actor = await make_actor("Partial", app_settings=cfg)
    assert (await actor.get("/capabilities")).json() == {"google_drive": "not_configured"}


async def test_drive_is_available_when_everything_is_set_and_no_value_is_leaked(make_actor):
    cfg = make_settings(**DRIVE)
    assert cfg.drive_configured is True
    actor = await make_actor("Full", app_settings=cfg)
    response = await actor.get("/capabilities")
    assert response.json() == {"google_drive": "available"}
    for value in DRIVE.values():
        assert value not in response.text


async def test_placeholder_values_do_not_count_as_configured():
    cfg = make_settings(**{**DRIVE, "google_picker_api_key": "your-api-key-here"})
    assert cfg.drive_configured is False


async def test_seed_session_creates_a_real_working_session(
    monkeypatch, make_actor, alice, sessionmaker, google, settings, engine
):
    from tests.helpers import make_client

    room_id = await new_room(alice)
    for key, value in {
        "DATABASE_URL": TEST_DATABASE_URL,
        "APP_ENV": "test",
        "CSRF_SECRET": TEST_CSRF_SECRET,
        "MESSAGE_FINGERPRINT_SECRET": TEST_FINGERPRINT_SECRET,
    }.items():
        monkeypatch.setenv(key, value)
    seeded = await seed_session.seed("Seeded Sam", room_id, "viewer")
    assert set(seeded) == {"user_id", "cookie_name", "secret"}
    async with make_client(settings, engine, google) as browser:
        put_cookie(browser, seeded["cookie_name"], seeded["secret"])
        me = await browser.get("/auth/me")
        assert (me.status_code, me.json()["display_name"]) == (200, "Seeded Sam")
        assert (await browser.get(f"/rooms/{room_id}")).json()["role"] == "viewer"
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM users WHERE display_name = 'Seeded Sam'")
        == 1
    )
    [session] = await rows(
        sessionmaker, "SELECT * FROM sessions WHERE user_id = :u", u=seeded["user_id"]
    )
    assert seeded["secret"] not in str(session)  # only a hash is stored


async def test_seed_session_refuses_production(monkeypatch):
    monkeypatch.setattr(seed_session, "load_settings", make_production_settings)
    with pytest.raises(SystemExit, match="production"):
        await seed_session.seed("Nope", None, "editor")
