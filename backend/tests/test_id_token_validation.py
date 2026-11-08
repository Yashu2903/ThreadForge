"""ID token validation: every way a token can be wrong must end in error=invalid_token with no
user, identity or session created. Production verification stays fully enabled."""

import time

import pytest

from tests.fake_google import OMIT
from tests.helpers import error_code, rows, scalar, sign_in

NOW = int(time.time())

BAD_TOKENS = {
    "bad_signature": {"signer": "wrong_key"},
    "alg_none": {"signer": "none"},
    "hs256_signed_with_public_key": {"signer": "hs256_public_key"},
    "unknown_kid": {"signer": "unknown_kid"},
    "wrong_issuer": {"iss": "https://evil.example"},
    "issuer_lookalike": {"iss": "https://accounts.google.com.evil.example"},
    "missing_issuer": {"iss": OMIT},
    "wrong_audience": {"aud": "someone-elses-client-id"},
    "audience_list_without_us": {"aud": ["a", "b"]},
    "multiple_audiences_without_azp": {
        "aud": ["test-client-id.apps.googleusercontent.com", "other"],
        "azp": OMIT,
    },
    "wrong_azp": {"azp": "someone-elses-client-id"},
    "expired": {"exp": NOW - 3600},
    "missing_exp": {"exp": OMIT},
    "issued_in_future": {"iat": NOW + 3600, "exp": NOW + 7200},
    "wrong_nonce": {"nonce": "N" * 43},
    "missing_nonce": {"nonce": OMIT},
    "missing_subject": {"sub": OMIT},
    "empty_subject": {"sub": ""},
    "numeric_subject": {"sub": 12345},
    "oversized_subject": {"sub": "s" * 256},
    "email_verified_string_true": {"email_verified": "true"},
    "email_verified_string_false": {"email_verified": "false"},
    "email_verified_number": {"email_verified": 1},
}


@pytest.mark.parametrize("spec", BAD_TOKENS.values(), ids=BAD_TOKENS.keys())
async def test_invalid_id_token_is_rejected_without_side_effects(
    client, google, sessionmaker, spec
):
    response = await sign_in(client, google, **spec)
    assert error_code(response) == "invalid_token"
    for table in ("users", "auth_identities", "sessions"):
        assert await scalar(sessionmaker, f"SELECT count(*) FROM {table}") == 0, table
    assert client.cookies.get("tf_session") is None


async def test_missing_id_token_in_response_is_invalid_token(client, google, sessionmaker):
    original = google.sign
    google.sign = lambda claims, signer: None  # token endpoint returns id_token: null
    try:
        response = await sign_in(client, google)
    finally:
        google.sign = original
    assert error_code(response) == "invalid_token"
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0


async def test_valid_token_from_the_same_setup_is_accepted(client, google):
    """Control: the parametrized failures above are due to the specific defect, not the setup."""
    response = await sign_in(client, google)
    assert error_code(response) is None


async def test_small_clock_skew_is_tolerated(client, google):
    response = await sign_in(client, google, exp=NOW - 10, iat=NOW - 3610)
    assert error_code(response) is None  # within the 30 s leeway


async def test_multiple_audiences_with_correct_azp_is_accepted(client, google):
    response = await sign_in(
        client, google, aud=["test-client-id.apps.googleusercontent.com", "other"]
    )
    assert error_code(response) is None


async def test_discovery_metadata_with_wrong_issuer_is_refused(client, google, sessionmaker):
    google.served_issuer = "https://evil.example"
    response = await client.get("/auth/login")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


async def test_jwks_refetched_once_after_key_rotation(client, google, sessionmaker):
    # First login primes the JWKS cache with key-1.
    assert error_code(await sign_in(client, google)) is None
    # Google rotates: new key published under a new kid, tokens now signed with it.
    from joserfc.jwk import RSAKey

    new_key = RSAKey.generate_key(2048, {"kid": "rotated"})
    google.jwks_keys = [new_key.as_dict(private=False)]
    google.key = new_key
    client._transport.app.state.oidc.jwks_min_refresh_seconds = 0  # allow an immediate refetch
    original_sign = google.sign

    def sign_with_new_kid(claims, signer):
        from joserfc import jwt

        return jwt.encode({"alg": "RS256", "kid": "rotated"}, claims, new_key)

    google.sign = sign_with_new_kid
    try:
        response = await sign_in(client, google)
    finally:
        google.sign = original_sign
    assert error_code(response) is None
    assert len(await rows(sessionmaker, "SELECT 1 FROM sessions WHERE revoked_at IS NULL")) == 1
