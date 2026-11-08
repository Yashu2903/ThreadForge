"""Google OpenID Connect client.

Division of labour:
  * Authlib's httpx OAuth2 client builds the authorization URL (state, nonce, PKCE S256 challenge)
    and exchanges the code (fetch_token with code_verifier).
  * joserfc (Authlib's maintained JOSE successor; authlib.jose is deprecated) verifies the ID
    token signature against Google's JWKS and validates the registered claims.
  * This module adds the OIDC rules Authlib's low-level client does not enforce for us:
    nonce, authorized party, issuer normalization, and strict profile-claim typing.

Nothing here touches PostgreSQL, so callers never hold a transaction open across a Google call.
Discovery metadata and JWKS are fetched lazily with explicit timeouts, so the app starts and
/health/ready passes with no internet access.

Never log tokens, codes, nonces or verifiers. Failures are logged by type/reason only.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx2
from authlib.integrations.base_client import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from app.config import GOOGLE_ISSUER, Settings
from app.logging_config import APP_LOGGER

log = logging.getLogger(f"{APP_LOGGER}.oidc")

SCOPE = "openid profile email"  # no offline access, no extra scopes
ALLOWED_ALGORITHMS = ("RS256",)  # Google signs ID tokens with RS256. Rejects "none" and HS*.
HTTP_TIMEOUT = httpx2.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
TOTAL_TIMEOUT_SECONDS = 15.0  # wall-clock bound for the whole code exchange + validation
METADATA_TTL_SECONDS = 3600.0
JWKS_TTL_SECONDS = 3600.0
MAX_JSON_BYTES = 1_000_000
CLOCK_LEEWAY_SECONDS = 30
FALLBACK_DISPLAY_NAME = "ThreadForge user"


class OIDCError(Exception):
    """Base class. `code` is the stable error code used in the callback error redirect."""

    code = "login_failed"


class ProviderUnavailable(OIDCError):
    code = "provider_unavailable"


class InvalidToken(OIDCError):
    code = "invalid_token"


class LoginFailed(OIDCError):
    code = "login_failed"


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str  # canonical issuer (see accepted_issuers)
    subject: str
    name: str | None
    email: str | None
    email_verified: bool = False


@dataclass
class _Cached:
    value: Any = None
    fetched_at: float = field(default=0.0)


def accepted_issuers(configured_issuer: str) -> frozenset[str]:
    """Issuer values we accept in an ID token and in discovery metadata.

    Google documents that ID tokens may carry either "https://accounts.google.com" or the bare
    "accounts.google.com". That is the *only* normalization: any other issuer must match the
    configured value exactly. Issuer validation is never disabled. Whichever form Google sends,
    we store the single canonical configured issuer, so one person is always one identity.
    """
    if configured_issuer == GOOGLE_ISSUER:
        return frozenset({GOOGLE_ISSUER, "accounts.google.com"})
    return frozenset({configured_issuer})


def _is_https(url: object) -> bool:
    return isinstance(url, str) and urlsplit(url).scheme == "https" and bool(urlsplit(url).netloc)


class OIDCClient:
    def __init__(
        self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._metadata = _Cached()
        self._jwks = _Cached()
        self._lock = asyncio.Lock()
        self._issuers = accepted_issuers(settings.google_issuer)
        # Rate-limits JWKS refetches triggered by unknown key IDs (key rotation vs. abuse).
        self.jwks_min_refresh_seconds = 30.0

    # ---- discovery and keys (lazy, cached) --------------------------------------------------

    def _http(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            timeout=HTTP_TIMEOUT, transport=self._transport, follow_redirects=False
        )

    async def _get_json(self, url: str) -> dict[str, Any]:
        async with self._http() as http:
            response = await http.get(url, headers={"Accept": "application/json"})
        if response.status_code != 200 or len(response.content) > MAX_JSON_BYTES:
            raise ProviderUnavailable(f"unexpected response ({response.status_code})")
        data = response.json()
        if not isinstance(data, dict):
            raise ProviderUnavailable("response was not a JSON object")
        return data

    async def metadata(self) -> dict[str, Any]:
        async with self._lock:
            age = time.monotonic() - self._metadata.fetched_at
            if self._metadata.value is not None and age < METADATA_TTL_SECONDS:
                return self._metadata.value
            try:
                data = await self._get_json(self._settings.google_discovery_url)
            except OIDCError:
                raise
            except (httpx2.HTTPError, ValueError, TimeoutError) as exc:
                log.warning("discovery fetch failed: %s", type(exc).__name__)
                raise ProviderUnavailable("discovery failed") from None
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not _is_https(data.get(key)):
                    raise ProviderUnavailable(f"metadata {key} missing or not https")
            if data.get("issuer") not in self._issuers:
                raise ProviderUnavailable("metadata issuer mismatch")
            self._metadata = _Cached(data, time.monotonic())
            return data

    async def _key_set(self, *, force: bool = False) -> KeySet:
        meta = await self.metadata()
        async with self._lock:
            age = time.monotonic() - self._jwks.fetched_at
            have = self._jwks.value is not None
            if have and age < JWKS_TTL_SECONDS and not force:
                return self._jwks.value
            if have and force and age < self.jwks_min_refresh_seconds:
                return self._jwks.value
            try:
                data = await self._get_json(meta["jwks_uri"])
                key_set = KeySet.import_key_set(data)  # type: ignore[arg-type]
            except OIDCError:
                raise
            except (httpx2.HTTPError, ValueError, TimeoutError, JoseError, KeyError) as exc:
                log.warning("jwks fetch failed: %s", type(exc).__name__)
                raise ProviderUnavailable("jwks failed") from None
            self._jwks = _Cached(key_set, time.monotonic())
            return key_set

    # ---- step 1: authorization URL ------------------------------------------------------------

    async def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str:
        """Authorization-code request with our own state and nonce and an S256 PKCE challenge."""
        meta = await self.metadata()
        async with AsyncOAuth2Client(
            client_id=self._settings.google_client_id,
            scope=SCOPE,
            redirect_uri=self._settings.oidc_callback_url,
            code_challenge_method="S256",
            transport=self._transport,
        ) as client:
            url, _ = client.create_authorization_url(
                meta["authorization_endpoint"],
                state=state,
                code_verifier=code_verifier,  # Authlib derives the S256 code_challenge from it
                nonce=nonce,
                prompt="select_account",
            )
        return url

    # ---- step 2: code exchange + ID token validation -----------------------------------------

    async def complete_login(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> VerifiedIdentity:
        """Exchange the code (with the PKCE verifier) and return the verified identity.

        Raises an OIDCError subclass for every failure. Google's access/refresh tokens are
        discarded: only the ID token is read, and nothing is persisted.
        """
        if not self._settings.oidc_configured:
            raise LoginFailed("Google credentials are not configured")
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                meta = await self.metadata()
                token = await self._exchange_code(meta["token_endpoint"], code, code_verifier)
                id_token = token.get("id_token")
                if not isinstance(id_token, str) or not id_token:
                    raise InvalidToken("no id_token in token response")
                del token
                return await self._validate(id_token, nonce)
        except OIDCError:
            raise
        except TimeoutError:
            raise ProviderUnavailable("timed out") from None
        except httpx2.HTTPStatusError as exc:
            # 5xx = provider trouble; 4xx = Google rejected our request.
            if exc.response.status_code >= 500:
                raise ProviderUnavailable("provider error") from None
            raise LoginFailed("provider rejected the request") from None
        except httpx2.HTTPError as exc:
            log.warning("token exchange network failure: %s", type(exc).__name__)
            raise ProviderUnavailable("network failure") from None
        except OAuthError as exc:
            # e.g. invalid_grant: expired/used code, wrong verifier or redirect_uri.
            log.warning(
                "token endpoint rejected exchange: %s", getattr(exc, "error", "oauth_error")
            )
            raise LoginFailed("token endpoint error") from None
        except Exception as exc:
            log.warning("unexpected login failure: %s", type(exc).__name__)
            raise LoginFailed("unexpected failure") from None

    async def _exchange_code(self, token_endpoint: str, code: str, code_verifier: str) -> dict:
        async with AsyncOAuth2Client(
            client_id=self._settings.google_client_id,
            client_secret=self._settings.google_client_secret.get_secret_value(),
            scope=SCOPE,
            redirect_uri=self._settings.oidc_callback_url,
            code_challenge_method="S256",
            timeout=HTTP_TIMEOUT,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            return await client.fetch_token(
                token_endpoint,
                code=code,
                code_verifier=code_verifier,
                grant_type="authorization_code",
            )

    async def _validate(self, id_token: str, nonce: str) -> VerifiedIdentity:
        claims: dict[str, Any] = {}
        for attempt in (0, 1):
            keys = await self._key_set(force=attempt == 1)
            try:
                # algorithms= is a strict allow-list: "none" and HS256 (public-key-as-HMAC-secret
                # confusion) are rejected before any key is used.
                claims = jwt.decode(id_token, keys, algorithms=list(ALLOWED_ALGORITHMS)).claims
                break
            except InvalidKeyIdError:
                if attempt == 0:
                    continue  # unknown kid: Google may have rotated keys; refetch once
                raise InvalidToken("unknown signing key") from None
            except (JoseError, ValueError, TypeError, KeyError):
                raise InvalidToken("signature or algorithm rejected") from None
        return self._verify_claims(claims, nonce)

    def _verify_claims(self, claims: dict[str, Any], nonce: str) -> VerifiedIdentity:
        client_id = self._settings.google_client_id
        registry = JWTClaimsRegistry(
            leeway=CLOCK_LEEWAY_SECONDS,
            iss={"essential": True, "values": sorted(self._issuers)},
            aud={"essential": True, "value": client_id},
            sub={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
        )
        try:
            registry.validate(claims)
        except (JoseError, ValueError, TypeError):
            raise InvalidToken("registered claims rejected") from None

        # Authorized party (OIDC Core 3.1.3.7): required when the token has several audiences,
        # and must be us whenever present.
        audience = claims.get("aud")
        azp = claims.get("azp")
        if isinstance(audience, list) and len(audience) > 1 and azp is None:
            raise InvalidToken("azp required for multiple audiences")
        if azp is not None and azp != client_id:
            raise InvalidToken("azp mismatch")

        # Nonce binds this ID token to this login attempt (replay protection).
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            raise InvalidToken("nonce mismatch")

        subject = claims["sub"]
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            raise InvalidToken("bad subject")

        # email_verified must be a real JSON boolean. Some providers historically sent the
        # string "true"; accepting strings invites "false"-is-truthy bugs, so we reject them.
        email_verified = claims.get("email_verified", False)
        if not isinstance(email_verified, bool):
            raise InvalidToken("email_verified is not a boolean")

        email = claims.get("email")
        email = email if isinstance(email, str) and 1 <= len(email) <= 320 else None
        name = claims.get("name")
        return VerifiedIdentity(
            issuer=self._settings.google_issuer,
            subject=subject,
            name=name if isinstance(name, str) else None,
            email=email,
            email_verified=email_verified and email is not None,
        )
