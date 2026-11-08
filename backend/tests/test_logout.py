"""POST /auth/logout: revocation, CSRF/Origin enforcement, idempotence, cookie clearing."""

import pytest

from tests.factories import make_production_settings
from tests.helpers import execute, make_client, put_cookie, scalar, sign_in, start_login

ORIGIN = "http://localhost:8000"


@pytest.fixture
async def signed_in(client, google):
    await sign_in(client, google)
    return client


async def csrf_headers(client, origin=ORIGIN):
    token = (await client.get("/auth/csrf")).json()["csrf_token"]
    headers = {"X-CSRF-Token": token}
    if origin is not None:
        headers["Origin"] = origin
    return headers


async def live_sessions(sessionmaker):
    return await scalar(sessionmaker, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL")


async def test_logout_revokes_in_database_clears_cookie_and_blocks_reuse(signed_in, sessionmaker):
    old_secret = signed_in.cookies.get("tf_session")
    response = await signed_in.post("/auth/logout", headers=await csrf_headers(signed_in))

    assert response.status_code == 204
    assert response.content == b""
    assert await live_sessions(sessionmaker) == 0
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL")
        == 1
    )

    cleared = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith("tf_session=")
    )
    assert "Max-Age=0" in cleared and "Path=/" in cleared and "HttpOnly" in cleared
    assert "SameSite=lax" in cleared and "Domain" not in cleared
    assert response.headers["cache-control"] == "no-store"

    assert signed_in.cookies.get("tf_session") is None
    assert (await signed_in.get("/auth/me")).status_code == 401
    # Replaying the exact old cookie value must fail too: the server-side row is revoked.
    put_cookie(signed_in, "tf_session", old_secret)
    assert (await signed_in.get("/auth/me")).status_code == 401
    assert (await signed_in.get("/auth/csrf")).status_code == 401


async def test_repeated_logout_is_safe(signed_in):
    headers = await csrf_headers(signed_in)
    assert (await signed_in.post("/auth/logout", headers=headers)).status_code == 204
    assert (await signed_in.post("/auth/logout", headers=headers)).status_code == 204
    assert (await signed_in.post("/auth/logout")).status_code == 204


async def test_logout_with_stale_cookie_returns_204_and_clears_it(client, google, sessionmaker):
    await sign_in(client, google)
    stale = client.cookies.get("tf_session")
    await execute(sessionmaker, "UPDATE sessions SET revoked_at = now()")
    put_cookie(client, "tf_session", stale)
    response = await client.post("/auth/logout")  # no CSRF/Origin needed: nothing to protect
    assert response.status_code == 204
    assert any(
        c.startswith("tf_session=") and "Max-Age=0" in c
        for c in response.headers.get_list("set-cookie")
    )


@pytest.mark.parametrize("garbage", ["", "x", "A" * 43, "!" * 60, "a" * 9000])
async def test_logout_never_fails_on_garbage_cookies(client, garbage):
    put_cookie(client, "tf_session", garbage)
    assert (await client.post("/auth/logout")).status_code == 204


async def test_logout_without_any_session_is_204(client):
    assert (await client.post("/auth/logout")).status_code == 204


# ---- CSRF and Origin enforcement --------------------------------------------------------------


async def assert_forbidden_and_still_signed_in(client, sessionmaker, response):
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "csrf_failed"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert not any(c.startswith("tf_session=") for c in response.headers.get_list("set-cookie"))
    assert await live_sessions(sessionmaker) == 1  # NOT revoked
    assert (await client.get("/auth/me")).status_code == 200  # still signed in


async def test_missing_csrf_token_is_403_and_does_not_revoke(signed_in, sessionmaker):
    response = await signed_in.post("/auth/logout", headers={"Origin": ORIGIN})
    await assert_forbidden_and_still_signed_in(signed_in, sessionmaker, response)


@pytest.mark.parametrize("bad_token", ["", "nope", "0" * 64, "A" * 500])
async def test_invalid_csrf_token_is_403_and_does_not_revoke(signed_in, sessionmaker, bad_token):
    response = await signed_in.post(
        "/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": bad_token}
    )
    await assert_forbidden_and_still_signed_in(signed_in, sessionmaker, response)


async def test_csrf_token_from_another_session_is_403(engine, settings, google, sessionmaker):
    async with (
        make_client(settings, engine, google) as a,
        make_client(settings, engine, google) as b,
    ):
        await sign_in(a, google)
        await sign_in(b, google)
        b_headers = await csrf_headers(b)
        response = await a.post("/auth/logout", headers=b_headers)
        assert response.status_code == 403
    assert await live_sessions(sessionmaker) == 2


async def test_missing_origin_is_403_even_with_valid_token(signed_in, sessionmaker):
    headers = await csrf_headers(signed_in, origin=None)
    response = await signed_in.post("/auth/logout", headers=headers)
    await assert_forbidden_and_still_signed_in(signed_in, sessionmaker, response)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",
        "http://localhost:8001",
        "http://localhost",
        "https://localhost:8000",
        "http://localhost:8000.evil.example",
        "http://evil.example:8000",
    ],
)
async def test_untrusted_origin_is_403_even_with_valid_token(signed_in, sessionmaker, origin):
    headers = await csrf_headers(signed_in, origin=origin)
    response = await signed_in.post("/auth/logout", headers=headers)
    await assert_forbidden_and_still_signed_in(signed_in, sessionmaker, response)


async def test_trusted_origin_match_is_case_insensitive_on_scheme_and_host(signed_in):
    headers = await csrf_headers(signed_in, origin="HTTP://LOCALHOST:8000")
    assert (await signed_in.post("/auth/logout", headers=headers)).status_code == 204


async def test_csrf_check_does_not_depend_on_cookie_alone(signed_in, sessionmaker):
    """The classic CSRF shape: cookie is sent automatically, but the attacker's page cannot add
    the token. Cookie + trusted-looking Origin without the token must fail."""
    response = await signed_in.post("/auth/logout", headers={"Origin": ORIGIN})
    assert response.status_code == 403
    assert await live_sessions(sessionmaker) == 1


async def test_logout_only_accepts_post(signed_in):
    assert (await signed_in.get("/auth/logout")).status_code == 405
    assert (await signed_in.put("/auth/logout")).status_code == 405
    assert (await signed_in.get("/auth/me")).status_code == 200


# ---- scope of logout ---------------------------------------------------------------------------


async def test_logout_affects_only_the_current_browser(engine, settings, google, sessionmaker):
    async with (
        make_client(settings, engine, google) as a,
        make_client(settings, engine, google) as b,
    ):
        await sign_in(a, google)
        await sign_in(b, google)
        assert (await a.post("/auth/logout", headers=await csrf_headers(a))).status_code == 204
        assert (await a.get("/auth/me")).status_code == 401
        assert (await b.get("/auth/me")).status_code == 200  # other device stays signed in
    assert await live_sessions(sessionmaker) == 1


async def test_disabled_user_can_still_log_out_their_session(signed_in, sessionmaker):
    token_headers = await csrf_headers(signed_in)
    await execute(sessionmaker, "UPDATE users SET state = 'disabled'")
    assert (await signed_in.get("/auth/me")).status_code == 401  # blocked from the API...
    response = await signed_in.post("/auth/logout", headers=token_headers)
    assert response.status_code == 204  # ...but can still end the session
    assert await live_sessions(sessionmaker) == 0


async def test_full_round_trip(client, google):
    assert (await client.get("/auth/me")).status_code == 401
    assert (await sign_in(client, google)).status_code == 303
    assert (await client.get("/auth/me")).status_code == 200
    assert (
        await client.post("/auth/logout", headers=await csrf_headers(client))
    ).status_code == 204
    assert (await client.get("/auth/me")).status_code == 401
    # And a login after logout works normally.
    assert (await sign_in(client, google)).status_code == 303
    assert (await client.get("/auth/me")).status_code == 200


# ---- production cookie attributes ---------------------------------------------------------------


async def test_production_session_cookie_attributes_on_login_and_logout(engine, google):
    settings = make_production_settings()
    google.callback_url = settings.oidc_callback_url
    google.client_id = settings.google_client_id
    google.client_secret = settings.google_client_secret.get_secret_value()
    origin = "https://app.threadforge.test"
    async with make_client(
        settings, engine, google, base_url="https://api.threadforge.test"
    ) as client:
        login = await start_login(client)
        code, state = google.authorize(login.headers["location"])
        callback = await client.get("/auth/callback", params={"code": code, "state": state})
        me = await client.get("/auth/me")
        token = (await client.get("/auth/csrf")).json()["csrf_token"]
        logout = await client.post(
            "/auth/logout", headers={"X-CSRF-Token": token, "Origin": origin}
        )

    assert me.status_code == 200
    session_cookie = next(c for c in callback.headers.get_list("set-cookie") if "tf_session" in c)
    cleared = next(c for c in logout.headers.get_list("set-cookie") if "tf_session" in c)
    for header in (session_cookie, cleared):
        assert header.startswith("__Host-tf_session=")
        for attribute in ("HttpOnly", "Secure", "Path=/", "SameSite=lax"):
            assert attribute in header, (attribute, header)
        assert "Domain" not in header
    assert "Max-Age=0" in cleared
    assert logout.status_code == 204
