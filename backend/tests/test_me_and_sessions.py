"""Session validation through /auth/me and /auth/csrf."""

import hashlib
import hmac

import pytest

from app.services.sessions import AuthContext, AuthUser
from tests.factories import TEST_CSRF_SECRET, make_settings
from tests.helpers import execute, make_client, put_cookie, rows, scalar, sign_in


@pytest.fixture
async def signed_in(client, google):
    response = await sign_in(client, google)
    assert response.status_code == 303
    return client


def assert_unauthenticated(response):
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    return body


async def test_active_session_reads_me_with_only_the_four_safe_fields(signed_in):
    response = await signed_in.get("/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "display_name", "email", "email_verified"}
    assert body["display_name"] == "Ada Lovelace"
    assert body["email"] == "ada@example.com"
    assert body["email_verified"] is True
    assert response.headers["cache-control"] == "no-store"
    assert "x-request-id" in response.headers


async def test_injected_cookies_are_really_sent(signed_in, engine, settings, google):
    """Guard against vacuous tests: a cookie placed with put_cookie reaches the server and
    authenticates. If this failed, every 'rejected cookie' test below could pass for the wrong
    reason (the cookie never being sent at all)."""
    secret = signed_in.cookies.get("tf_session")
    async with make_client(settings, engine, google) as other_browser:
        assert (await other_browser.get("/auth/me")).status_code == 401
        put_cookie(other_browser, "tf_session", secret)
        assert (await other_browser.get("/auth/me")).status_code == 200


async def test_missing_cookie_is_401(client):
    assert_unauthenticated(await client.get("/auth/me"))


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "x",
        "short",
        "a" * 42,
        "!" * 43,
        "a b" * 20,
        "%C3%A9" * 20,
        "a" * 129,
        "a" * 5000,
        "../../etc",
    ],
)
async def test_malformed_cookie_is_401_and_never_crashes(client, garbage):
    put_cookie(client, "tf_session", garbage)
    assert_unauthenticated(await client.get("/auth/me"))


async def test_unknown_but_well_formed_secret_is_401(client):
    put_cookie(client, "tf_session", "A" * 43)
    assert_unauthenticated(await client.get("/auth/me"))


async def test_expired_session_is_401(signed_in, sessionmaker):
    await execute(
        sessionmaker,
        "UPDATE sessions SET created_at = now() - interval '8 days', "
        "expires_at = now() - interval '1 minute'",
    )
    assert_unauthenticated(await signed_in.get("/auth/me"))


async def test_revoked_session_is_401(signed_in, sessionmaker):
    await execute(sessionmaker, "UPDATE sessions SET revoked_at = now()")
    assert_unauthenticated(await signed_in.get("/auth/me"))


@pytest.mark.parametrize("state", ["disabled", "deleted"])
async def test_valid_session_of_inactive_user_is_indistinguishable_from_no_session(
    signed_in, client, sessionmaker, state
):
    anonymous = (await make_client_anonymous(client)).json()
    await execute(sessionmaker, "UPDATE users SET state = :s", s=state)
    for path in ("/auth/me", "/auth/csrf"):
        response = await signed_in.get(path)
        body = assert_unauthenticated(response)
        assert body["error"]["code"] == anonymous["error"]["code"]
        assert body["error"]["message"] == anonymous["error"]["message"]


async def make_client_anonymous(client):
    """What a request with no session at all returns (fresh cookie jar, same app)."""
    import httpx2

    async with httpx2.AsyncClient(
        transport=client._transport, base_url="http://localhost:8000"
    ) as anonymous:
        return await anonymous.get("/auth/me")


async def test_database_stores_a_hash_that_differs_from_the_cookie_secret(signed_in, sessionmaker):
    secret = signed_in.cookies.get("tf_session")
    [row] = await rows(sessionmaker, "SELECT * FROM sessions")
    assert row["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert row["secret_hash"] != secret
    assert secret not in " ".join(str(v) for v in row.values())


async def test_sessions_have_independent_random_secrets(engine, settings, google):
    async with (
        make_client(settings, engine, google) as a,
        make_client(settings, engine, google) as b,
    ):
        await sign_in(a, google)
        await sign_in(b, google)
        assert a.cookies.get("tf_session") != b.cookies.get("tf_session")
        assert (await a.get("/auth/me")).status_code == 200
        assert (await b.get("/auth/me")).status_code == 200


async def test_new_login_invalidates_the_old_cookie(signed_in, google):
    old_secret = signed_in.cookies.get("tf_session")
    await sign_in(signed_in, google)
    assert signed_in.cookies.get("tf_session") != old_secret
    assert (await signed_in.get("/auth/me")).status_code == 200
    put_cookie(signed_in, "tf_session", old_secret)
    assert_unauthenticated(await signed_in.get("/auth/me"))


# ---- CSRF token endpoint --------------------------------------------------------------------


async def test_csrf_requires_authentication(client):
    assert_unauthenticated(await client.get("/auth/csrf"))


async def test_csrf_token_is_hmac_of_the_session_secret(signed_in):
    secret = signed_in.cookies.get("tf_session")
    expected = hmac.new(
        TEST_CSRF_SECRET.encode(), f"csrf-v1:{secret}".encode(), hashlib.sha256
    ).hexdigest()
    response = await signed_in.get("/auth/csrf")
    assert response.status_code == 200
    assert response.json() == {"csrf_token": expected}
    assert response.headers["cache-control"] == "no-store"
    assert secret not in response.text


async def test_csrf_token_is_stable_per_session_and_differs_between_sessions(
    engine, settings, google
):
    async with (
        make_client(settings, engine, google) as a,
        make_client(settings, engine, google) as b,
    ):
        await sign_in(a, google)
        await sign_in(b, google)
        a1 = (await a.get("/auth/csrf")).json()["csrf_token"]
        a2 = (await a.get("/auth/csrf")).json()["csrf_token"]
        b1 = (await b.get("/auth/csrf")).json()["csrf_token"]
    assert a1 == a2
    assert a1 != b1


async def test_csrf_token_depends_on_the_server_secret(engine, google, signed_in):
    secret = signed_in.cookies.get("tf_session")
    token = (await signed_in.get("/auth/csrf")).json()["csrf_token"]
    other = make_settings(csrf_secret="a-completely-different-csrf-secret-value-0123456789")
    async with make_client(other, engine, google) as other_app:
        put_cookie(other_app, "tf_session", secret)
        other_token = (await other_app.get("/auth/csrf")).json()["csrf_token"]
    assert token != other_token


def test_auth_context_repr_masks_the_session_secret():
    import uuid

    context = AuthContext(
        user=AuthUser(uuid.uuid4(), "Ada", None, False, "active"),
        session_id=uuid.uuid4(),
        session_secret="SUPER-SECRET-SESSION-VALUE",
    )
    assert "SUPER-SECRET" not in repr(context)
    assert "SUPER-SECRET" not in str(context)
    assert "SUPER-SECRET" not in f"{context!r}"


async def test_me_does_not_write_to_the_database(signed_in, sessionmaker):
    """No sliding expiry and no last-seen writes in this part."""
    [before] = await rows(sessionmaker, "SELECT * FROM sessions")
    for _ in range(3):
        await signed_in.get("/auth/me")
    [after] = await rows(sessionmaker, "SELECT * FROM sessions")
    assert before == after
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 1
