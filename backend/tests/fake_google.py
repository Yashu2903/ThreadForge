"""A stand-in for Google at the HTTP level (discovery, JWKS, token endpoint).

The application's real OIDC code runs unmodified against this: Authlib builds the authorization
URL and posts the token request, joserfc verifies signatures made with locally generated RSA
keys. The token endpoint enforces what Google enforces: client authentication, single-use codes,
matching redirect_uri, and the PKCE S256 verifier.

This proves our integration logic. It does NOT prove the real Google integration works; that
needs real credentials (see README, manual checklist).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://accounts.google.com"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

OMIT = object()  # claim override meaning "leave this claim out"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class _PendingCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    nonce: str
    claims: dict[str, Any]
    signer: str
    token_extra: dict[str, Any]


@dataclass
class FakeGoogle:
    client_id: str
    client_secret: str
    callback_url: str
    key: RSAKey = field(default_factory=lambda: RSAKey.generate_key(2048, {"kid": "key-1"}))
    other_key: RSAKey = field(default_factory=lambda: RSAKey.generate_key(2048, {"kid": "key-1"}))
    codes: dict[str, _PendingCode] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    token_forms: list[dict[str, str]] = field(default_factory=list)
    # Failure injection.
    discovery_status: int = 200
    token_status: int | None = None
    token_body: Any = None
    token_raises: Exception | None = None
    jwks_keys: list[dict[str, Any]] | None = None
    served_issuer: str = ISSUER

    def __post_init__(self) -> None:
        self.transport = httpx2.MockTransport(self._handle)

    # ---- simulating the browser's trip to Google -------------------------------------------

    def authorize(
        self,
        authorization_url: str,
        *,
        signer: str = "rsa",
        token_extra: dict[str, Any] | None = None,
        **claim_overrides: Any,
    ) -> tuple[str, str]:
        """Pretend the user signed in at Google. Returns (code, state) for the callback."""
        parts = urlsplit(authorization_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTH_ENDPOINT
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["response_type"] == "code"
        assert q["client_id"] == self.client_id
        assert q["redirect_uri"] == self.callback_url
        assert q["scope"] == "openid profile email"
        assert q["code_challenge_method"] == "S256"
        assert "access_type" not in q  # no offline access
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.served_issuer,
            "sub": "google-subject-1",
            "aud": self.client_id,
            "azp": self.client_id,
            "iat": now,
            "exp": now + 3600,
            "nonce": q["nonce"],
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "email_verified": True,
        }
        for name, value in claim_overrides.items():
            if value is OMIT:
                claims.pop(name, None)
            else:
                claims[name] = value
        code = secrets.token_urlsafe(24)
        self.codes[code] = _PendingCode(
            client_id=q["client_id"],
            redirect_uri=q["redirect_uri"],
            code_challenge=q["code_challenge"],
            nonce=q["nonce"],
            claims=claims,
            signer=signer,
            token_extra=token_extra or {},
        )
        return code, q["state"]

    # ---- token signing -----------------------------------------------------------------------

    def sign(self, claims: dict[str, Any], signer: str) -> str:
        header = {"alg": "RS256", "kid": "key-1"}
        if signer == "rsa":
            return jwt.encode(header, claims, self.key)
        if signer == "wrong_key":  # right kid, signed by a key Google never published
            return jwt.encode(header, claims, self.other_key)
        if signer == "unknown_kid":
            return jwt.encode({"alg": "RS256", "kid": "nope"}, claims, self.key)
        signing_input = (
            f"{_b64(json.dumps(self._hdr(signer)).encode())}.{_b64(json.dumps(claims).encode())}"
        )
        if signer == "none":
            return signing_input + "."
        if signer == "hs256_public_key":
            # Classic algorithm confusion: HMAC-sign with the PUBLIC key's PEM as the secret.
            secret = self.key.as_pem(private=False)
            mac = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
            return f"{signing_input}.{_b64(mac)}"
        raise AssertionError(signer)

    @staticmethod
    def _hdr(signer: str) -> dict[str, str]:
        return {"alg": "none" if signer == "none" else "HS256", "typ": "JWT", "kid": "key-1"}

    # ---- the HTTP surface --------------------------------------------------------------------

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url).split("?")[0]
        self.requests.append((request.method, url))
        if url == DISCOVERY_URL:
            if self.discovery_status != 200:
                return httpx2.Response(self.discovery_status, text="down")
            return httpx2.Response(
                200,
                json={
                    "issuer": self.served_issuer,
                    "authorization_endpoint": AUTH_ENDPOINT,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                },
            )
        if url == JWKS_URI:
            keys = self.jwks_keys or [self.key.as_dict(private=False)]
            return httpx2.Response(200, json={"keys": keys})
        if url == TOKEN_ENDPOINT:
            return self._token(request)
        return httpx2.Response(404)

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        if self.token_raises is not None:
            raise self.token_raises
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_forms.append(form)
        if self.token_status is not None:
            body = self.token_body if self.token_body is not None else {"error": "server_error"}
            if isinstance(body, str):
                return httpx2.Response(self.token_status, text=body)
            return httpx2.Response(self.token_status, json=body)

        # Client authentication (Authlib's default: client_secret_basic).
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx2.Response(401, json={"error": "invalid_client"})

        pending = self.codes.pop(form.get("code", ""), None)  # single use
        if pending is None or form.get("grant_type") != "authorization_code":
            return httpx2.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != pending.redirect_uri:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        if not 43 <= len(verifier) <= 128 or challenge != pending.code_challenge:
            return httpx2.Response(400, json={"error": "invalid_grant"})

        return httpx2.Response(
            200,
            json={
                "access_token": "fake-access-token-SHOULD-NEVER-BE-STORED",
                "refresh_token": "fake-refresh-token-SHOULD-NEVER-BE-STORED",
                "token_type": "Bearer",
                "expires_in": 3599,
                "scope": "openid email profile",
                "id_token": self.sign(pending.claims, pending.signer),
                **pending.token_extra,
            },
        )
