"""Login start, callback, account resolution and session creation (happy paths and rules)."""

import asyncio
import logging
from urllib.parse import parse_qs, urlsplit

import httpx2

from app.services.security import sha256_hex
from tests.factories import make_production_settings, make_settings
from tests.fake_google import AUTH_ENDPOINT, OMIT
from tests.helpers import error_code, execute, make_client, rows, scalar, sign_in, start_login

SUCCESS = "http://localhost:8000/auth/me"


# ---- GET /auth/login ------------------------------------------------------------------------


async def test_login_redirects_to_google_with_all_security_parameters(client, google):
    response = await start_login(client)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTH_ENDPOINT
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert q["response_type"] == "code"
    assert q["scope"] == "openid profile email"
    assert q["redirect_uri"] == "http://localhost:8000/auth/callback"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 43 and len(q["nonce"]) >= 43 and len(q["code_challenge"]) >= 43
    assert "access_type" not in q  # no offline access / refresh tokens
    assert response.headers["cache-control"] == "no-store"


async def test_login_stores_hashes_not_raw_state_or_binding_secret(client, sessionmaker):
    response = await start_login(client)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    binding = client.cookies.get("tf_login_binding")
    [row] = await rows(sessionmaker, "SELECT * FROM oidc_login_attempts")
    assert row["state_hash"] == sha256_hex(state) != state
    assert row["browser_binding_hash"] == sha256_hex(binding) != binding
    assert row["consumed_at"] is None
    assert 43 <= len(row["pkce_verifier"]) <= 128
    assert row["expires_at"] > row["created_at"]


async def test_login_sets_binding_cookie_attributes_in_development(client):
    response = await start_login(client)
    header = response.headers["set-cookie"]
    assert header.startswith("tf_login_binding=")
    assert "HttpOnly" in header and "Path=/" in header and "SameSite=lax" in header
    assert "Max-Age=600" in header
    assert "Domain" not in header and "Secure" not in header  # plain localhost HTTP dev


async def test_login_without_google_credentials_is_a_clear_503(engine, google):
    settings = make_settings(google_client_id="", google_client_secret="")
    async with make_client(settings, engine, google) as client:
        response = await start_login(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "oidc_not_configured"


async def test_login_when_google_unreachable_is_503_and_writes_nothing(
    client, google, sessionmaker
):
    google.discovery_status = 500
    response = await start_login(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert await scalar(sessionmaker, "SELECT count(*) FROM oidc_login_attempts") == 0


async def test_app_needs_no_network_at_startup_or_for_readiness(client, google):
    assert (await client.get("/health/ready")).status_code == 200
    assert google.requests == []  # nothing contacted Google


# ---- successful sign-in ----------------------------------------------------------------------


async def test_new_user_login_creates_user_identity_and_session(client, google, sessionmaker):
    response = await sign_in(client, google)
    assert response.status_code == 303
    assert response.headers["location"] == SUCCESS
    assert response.headers["cache-control"] == "no-store"

    [user] = await rows(sessionmaker, "SELECT * FROM users")
    assert user["display_name"] == "Ada Lovelace"
    assert user["email"] == "ada@example.com" and user["email_verified"] is True
    assert user["state"] == "active" and user["version"] == 0
    [identity] = await rows(sessionmaker, "SELECT * FROM auth_identities")
    assert identity["user_id"] == user["id"]
    assert identity["issuer"] == "https://accounts.google.com"
    assert identity["subject"] == "google-subject-1"
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 1


async def test_session_cookie_set_binding_cookie_cleared_and_only_hash_stored(
    client, google, sessionmaker
):
    response = await sign_in(client, google)
    cookies = response.headers.get_list("set-cookie")
    session_header = next(c for c in cookies if c.startswith("tf_session="))
    assert "HttpOnly" in session_header and "Path=/" in session_header
    assert "SameSite=lax" in session_header and "Max-Age=604800" in session_header
    assert "Domain" not in session_header
    binding_header = next(c for c in cookies if c.startswith("tf_login_binding="))
    assert "Max-Age=0" in binding_header  # cleared

    secret = client.cookies.get("tf_session")
    assert secret and len(secret) >= 43
    [row] = await rows(sessionmaker, "SELECT * FROM sessions")
    assert row["secret_hash"] == sha256_hex(secret) != secret
    assert secret not in str(await rows(sessionmaker, "SELECT * FROM sessions"))
    lifetime = (row["expires_at"] - row["created_at"]).total_seconds()
    assert 604_790 < lifetime < 604_810  # seven days, absolute


async def test_google_tokens_are_never_persisted(client, google, sessionmaker):
    await sign_in(client, google)
    dump = ""
    for table in ("users", "auth_identities", "sessions", "oidc_login_attempts"):
        dump += str(await rows(sessionmaker, f"SELECT * FROM {table}"))
    assert "fake-access-token" not in dump and "fake-refresh-token" not in dump
    assert "eyJ" not in dump  # no JWT of any kind


async def test_returning_login_resolves_same_user_and_refreshes_only_provider_email(
    client, google, sessionmaker
):
    await sign_in(client, google)
    [before] = await rows(sessionmaker, "SELECT * FROM users")

    await sign_in(
        client,
        google,
        name="Totally Different Name",
        email="new-address@example.org",
        email_verified=False,
    )
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 1
    assert await scalar(sessionmaker, "SELECT count(*) FROM auth_identities") == 1
    [after] = await rows(sessionmaker, "SELECT * FROM users")
    assert after["id"] == before["id"]
    assert after["display_name"] == "Ada Lovelace"  # never overwritten
    assert after["email"] == "ada@example.com"  # never overwritten
    assert after["email_verified"] is True
    assert after["version"] == before["version"]
    [identity] = await rows(sessionmaker, "SELECT * FROM auth_identities")
    assert identity["provider_email"] == "new-address@example.org"
    assert identity["provider_email_verified"] is False


async def test_same_email_different_subject_is_a_different_account(client, google, sessionmaker):
    await sign_in(client, google, sub="subject-A")
    await sign_in(client, google, sub="subject-B")  # same email ada@example.com
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 2
    assert await scalar(sessionmaker, "SELECT count(*) FROM auth_identities") == 2


async def test_display_name_falls_back_when_provider_sends_no_name(client, google, sessionmaker):
    await sign_in(client, google, name=OMIT, email=OMIT, email_verified=OMIT)
    [user] = await rows(sessionmaker, "SELECT * FROM users")
    assert user["display_name"] == "ThreadForge user"
    assert user["email"] is None and user["email_verified"] is False


async def test_overlong_or_blank_provider_names_are_made_safe(client, google, sessionmaker):
    await sign_in(client, google, sub="long", name="  " + "é" * 300 + "\x00\x07 ")
    await sign_in(client, google, sub="blank", name="   \x00  ")
    names = {r["display_name"] for r in await rows(sessionmaker, "SELECT display_name FROM users")}
    assert "ThreadForge user" in names
    assert all(1 <= len(n) <= 100 and "\x00" not in n for n in names)


async def test_both_google_issuer_forms_are_accepted_and_normalised(client, google, sessionmaker):
    await sign_in(client, google, sub="s", iss="https://accounts.google.com")
    await sign_in(client, google, sub="s", iss="accounts.google.com")
    [identity] = await rows(sessionmaker, "SELECT * FROM auth_identities")
    assert identity["issuer"] == "https://accounts.google.com"  # one canonical form stored
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 1


async def test_pkce_verifier_and_client_auth_reach_the_token_endpoint(client, google):
    await sign_in(client, google)
    [form] = google.token_forms
    assert form["grant_type"] == "authorization_code"
    assert 43 <= len(form["code_verifier"]) <= 128
    assert "client_secret" not in form  # sent via HTTP Basic, not in the body


async def test_redirect_targets_are_fixed_and_ignore_query_parameters(client, google):
    login = await start_login(client)
    code, state = google.authorize(login.headers["location"])
    response = await client.get(
        "/auth/callback",
        params={
            "code": code,
            "state": state,
            "next": "https://evil.example/",
            "redirect_uri": "https://evil.example/",
        },
    )
    assert response.headers["location"] == SUCCESS


# ---- login rotation ---------------------------------------------------------------------------


async def test_new_login_revokes_the_previous_valid_session(client, google, sessionmaker):
    await sign_in(client, google)
    first_secret = client.cookies.get("tf_session")
    await sign_in(client, google)
    second_secret = client.cookies.get("tf_session")
    assert first_secret != second_secret

    state = {
        r["secret_hash"]: r["revoked_at"] is not None
        for r in await rows(sessionmaker, "SELECT secret_hash, revoked_at FROM sessions")
    }
    assert state[sha256_hex(first_secret)] is True  # revoked
    assert state[sha256_hex(second_secret)] is False  # live


async def test_login_does_not_touch_other_browsers_sessions(engine, settings, google, sessionmaker):
    async with (
        make_client(settings, engine, google) as a,
        make_client(settings, engine, google) as b,
    ):
        await sign_in(a, google)
        await sign_in(b, google)  # different browser, same account
        revoked = await scalar(
            sessionmaker, "SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL"
        )
    assert revoked == 0  # other devices stay signed in


# ---- login attempt validation -----------------------------------------------------------------


async def _start(client, google, **spec):
    login = await start_login(client)
    code, state = google.authorize(login.headers["location"], **spec)
    return code, state


async def test_missing_state_or_code_is_rejected(client, google):
    code, state = await _start(client, google)
    assert (
        error_code(await client.get("/auth/callback", params={"code": code}))
        == "invalid_login_attempt"
    )
    assert (
        error_code(await client.get("/auth/callback", params={"state": state}))
        == "invalid_login_attempt"
    )
    assert error_code(await client.get("/auth/callback")) == "invalid_login_attempt"


async def test_malformed_state_or_code_is_rejected(client, google):
    code, state = await _start(client, google)
    for params in (
        {"code": code, "state": "short"},
        {"code": code, "state": state + "!!"},
        {"code": "", "state": state},
        {"code": "x" * 3000, "state": state},
        {"code": "has space", "state": state},
    ):
        assert (
            error_code(await client.get("/auth/callback", params=params)) == "invalid_login_attempt"
        )


async def test_duplicate_parameters_are_rejected(client, google):
    code, state = await _start(client, google)
    response = await client.get(f"/auth/callback?code={code}&state={state}&state={state}")
    assert error_code(response) == "invalid_login_attempt"


async def test_unknown_state_is_rejected(client, google):
    code, _ = await _start(client, google)
    response = await client.get("/auth/callback", params={"code": code, "state": "A" * 43})
    assert error_code(response) == "invalid_login_attempt"


async def test_missing_binding_cookie_is_rejected_and_attempt_not_burned(client, google):
    code, state = await _start(client, google)
    client.cookies.clear()
    assert (
        error_code(await client.get("/auth/callback", params={"code": code, "state": state}))
        == "invalid_login_attempt"
    )


async def test_wrong_browser_cannot_complete_the_login(engine, settings, google, sessionmaker):
    async with (
        make_client(settings, engine, google) as victim,
        make_client(settings, engine, google) as attacker,
    ):
        code, state = await _start(victim, google)
        # The attacker obtained the callback URL (state + code) but has no binding cookie...
        response = await attacker.get("/auth/callback", params={"code": code, "state": state})
        assert error_code(response) == "invalid_login_attempt"
        # ...or has their own, different binding cookie.
        await start_login(attacker)
        response = await attacker.get("/auth/callback", params={"code": code, "state": state})
        assert error_code(response) == "invalid_login_attempt"
        assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0
        # The failed replays did not burn the real user's attempt.
        ok = await victim.get("/auth/callback", params={"code": code, "state": state})
        assert ok.headers["location"] == SUCCESS


async def test_expired_attempt_is_rejected(client, google, sessionmaker):
    code, state = await _start(client, google)
    await execute(
        sessionmaker,
        "UPDATE oidc_login_attempts SET created_at = now() - interval '1 hour', "
        "expires_at = now() - interval '1 minute'",
    )
    assert (
        error_code(await client.get("/auth/callback", params={"code": code, "state": state}))
        == "invalid_login_attempt"
    )


async def test_state_cannot_be_reused(client, google, sessionmaker):
    code, state = await _start(client, google)
    binding = client.cookies.get("tf_login_binding")
    first = await client.get("/auth/callback", params={"code": code, "state": state})
    assert first.headers["location"] == SUCCESS
    # Even with the genuine binding cookie put back, the attempt is single use.
    client.cookies.set("tf_login_binding", binding, domain="localhost")
    replay = await client.get("/auth/callback", params={"code": code, "state": state})
    assert error_code(replay) == "invalid_login_attempt"
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 1


async def test_only_one_active_attempt_per_browser(client, google):
    """Starting a second login overwrites the binding cookie and invalidates the first attempt."""
    code1, state1 = await _start(client, google)
    code2, state2 = await _start(client, google)
    first = await client.get("/auth/callback", params={"code": code1, "state": state1})
    assert error_code(first) == "invalid_login_attempt"
    second = await client.get("/auth/callback", params={"code": code2, "state": state2})
    assert second.headers["location"] == SUCCESS


async def test_two_concurrent_callbacks_with_same_state_yield_exactly_one_success(
    client, google, sessionmaker
):
    code, state = await _start(client, google)
    results = await asyncio.gather(
        *[client.get("/auth/callback", params={"code": code, "state": state}) for _ in range(2)]
    )
    outcomes = sorted("ok" if r.headers["location"] == SUCCESS else error_code(r) for r in results)
    assert outcomes == ["invalid_login_attempt", "ok"]
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 1
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM oidc_login_attempts WHERE consumed_at IS NOT NULL"
        )
        == 1
    )


async def test_binding_cookie_survives_pre_consumption_failures(client, google):
    """A stale or forged callback must not destroy a newer attempt's binding cookie."""
    await _start(client, google)
    response = await client.get("/auth/callback", params={"code": "abc", "state": "bad"})
    assert not any(
        c.startswith("tf_login_binding=") for c in response.headers.get_list("set-cookie")
    )
    assert client.cookies.get("tf_login_binding")


async def test_binding_cookie_cleared_once_attempt_is_consumed_even_on_failure(client, google):
    google.token_status = 503
    response = await sign_in(client, google)
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith("tf_login_binding=") and "Max-Age=0" in c for c in cookies)


# ---- provider problems ------------------------------------------------------------------------


async def test_provider_denial_redirects_with_access_denied(client, google, sessionmaker):
    await _start(client, google)
    response = await client.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert error_code(response) == "access_denied"
    assert response.headers["location"].startswith("http://localhost:8000/auth/me?")
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0


async def test_other_provider_errors_map_to_stable_codes(client):
    for provider_error, expected in (
        ("temporarily_unavailable", "provider_unavailable"),
        ("server_error", "provider_unavailable"),
        ("invalid_scope", "login_failed"),
        ("anything_else", "login_failed"),
    ):
        response = await client.get("/auth/callback", params={"error": provider_error})
        assert error_code(response) == expected


async def test_token_endpoint_server_error_is_provider_unavailable(client, google, sessionmaker):
    google.token_status = 503
    google.token_body = "<html>upstream secret page</html>"
    response = await sign_in(client, google)
    assert error_code(response) == "provider_unavailable"
    assert "upstream" not in response.headers["location"]
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 0


async def test_token_endpoint_rejection_is_login_failed(client, google, sessionmaker):
    google.token_status = 400
    google.token_body = {"error": "invalid_grant", "error_description": "bad code"}
    response = await sign_in(client, google)
    assert error_code(response) == "login_failed"
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0


async def test_token_endpoint_timeout_is_provider_unavailable(client, google, sessionmaker):
    google.token_raises = httpx2.ReadTimeout("slow")
    response = await sign_in(client, google)
    assert error_code(response) == "provider_unavailable"
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0


async def test_token_endpoint_connection_failure_is_provider_unavailable(client, google):
    google.token_raises = httpx2.ConnectError("refused")
    assert error_code(await sign_in(client, google)) == "provider_unavailable"


async def test_attempt_is_consumed_even_when_google_fails(client, google, sessionmaker):
    google.token_status = 503
    await sign_in(client, google)
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM oidc_login_attempts WHERE consumed_at IS NOT NULL"
        )
        == 1
    )


# ---- disabled accounts --------------------------------------------------------------------------


async def test_disabled_account_gets_no_session(client, google, sessionmaker):
    await sign_in(client, google)
    await execute(sessionmaker, "DELETE FROM sessions")
    await execute(sessionmaker, "UPDATE users SET state = 'disabled'")
    client.cookies.clear()
    response = await sign_in(client, google)
    assert error_code(response) == "account_disabled"
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0
    assert client.cookies.get("tf_session") is None


async def test_deleted_account_gets_no_session(client, google, sessionmaker):
    await sign_in(client, google)
    await execute(sessionmaker, "DELETE FROM sessions")
    await execute(sessionmaker, "UPDATE users SET state = 'deleted'")
    client.cookies.clear()
    assert error_code(await sign_in(client, google)) == "account_disabled"


# ---- login-attempt cap and cleanup --------------------------------------------------------------


async def test_pending_attempt_cap_returns_429_with_retry_after(engine, google, sessionmaker):
    settings = make_settings(max_pending_login_attempts=2)
    async with make_client(settings, engine, google) as client:
        assert (await start_login(client)).status_code == 303
        assert (await start_login(client)).status_code == 303
        blocked = await start_login(client)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "login_busy"
    assert blocked.json()["error"]["request_id"] == blocked.headers["x-request-id"]
    assert 1 <= int(blocked.headers["retry-after"]) <= 600
    assert await scalar(sessionmaker, "SELECT count(*) FROM oidc_login_attempts") == 2


async def test_cap_counts_only_unexpired_unconsumed_attempts(engine, google, sessionmaker):
    settings = make_settings(max_pending_login_attempts=1)
    async with make_client(settings, engine, google) as client:
        code, state = await _start(client, google)
        assert (await start_login(client)).status_code == 429  # one pending: full
        await client.get("/auth/callback", params={"code": code, "state": state})  # consume
        # (the second /auth/login above overwrote nothing: it was refused before setting cookies)
        assert (await start_login(client)).status_code == 303


async def test_login_cleans_up_expired_attempts(client, google, sessionmaker):
    await start_login(client)
    await execute(
        sessionmaker,
        "UPDATE oidc_login_attempts SET created_at = now() - interval '1 hour', "
        "expires_at = now() - interval '1 minute'",
    )
    await start_login(client)
    assert await scalar(sessionmaker, "SELECT count(*) FROM oidc_login_attempts") == 1
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM oidc_login_attempts WHERE expires_at > now()"
        )
        == 1
    )


# ---- production cookie attributes ----------------------------------------------------------------


async def test_production_binding_and_session_cookies_are_host_prefixed_and_secure(engine, google):
    settings = make_production_settings()
    google.callback_url = settings.oidc_callback_url
    google.client_id = settings.google_client_id
    google.client_secret = settings.google_client_secret.get_secret_value()
    async with make_client(
        settings, engine, google, base_url="https://api.threadforge.test"
    ) as client:
        login = await start_login(client)
        binding_header = login.headers["set-cookie"]
        code, state = google.authorize(login.headers["location"])
        callback = await client.get("/auth/callback", params={"code": code, "state": state})
    for header in (binding_header, *callback.headers.get_list("set-cookie")):
        assert header.startswith("__Host-tf_")
        assert "HttpOnly" in header and "Secure" in header
        assert "Path=/" in header and "SameSite=lax" in header
        assert "Domain" not in header
    session_header = next(c for c in callback.headers.get_list("set-cookie") if "tf_session" in c)
    assert session_header.startswith("__Host-tf_session=") and "Max-Age=604800" in session_header
    assert callback.headers["location"] == "https://app.threadforge.test/"


# ---- logs ----------------------------------------------------------------------------------------


async def test_no_secrets_appear_in_logs_during_login(client, google, caplog):
    caplog.set_level(logging.DEBUG)
    login = await start_login(client)
    q = {k: v[0] for k, v in parse_qs(urlsplit(login.headers["location"]).query).items()}
    binding = client.cookies.get("tf_login_binding")
    code, state = google.authorize(login.headers["location"])
    await client.get("/auth/callback", params={"code": code, "state": state})
    session_secret = client.cookies.get("tf_session")
    verifier = google.token_forms[0]["code_verifier"]

    secrets_ = [
        state,
        binding,
        q["nonce"],
        verifier,
        code,
        session_secret,
        q["code_challenge"],
        "fake-access-token",
        "test-client-secret-value",
    ]
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''} {r.args!r}" for r in caplog.records)
    assert logged  # we really did capture log output
    for secret in secrets_:
        assert secret and secret not in logged, f"secret leaked into logs: {secret[:6]}…"
