# ThreadForge
A collaborative AI workspace where teams turn conversations and source documents into cited drafts, edit together in real time, review AI revisions, and approve and export versioned documents.

## Status: Part 1 — backend foundation and authentication

Implemented: FastAPI app, PostgreSQL + Alembic, Google OpenID Connect login, local users and provider identities, PostgreSQL-backed browser sessions, CSRF-protected logout, health endpoints, request IDs, a cleanup command, and an automated test suite.

Not implemented (later parts): rooms, invitations, memberships, messages, WebSockets, Redis, outbox, documents, AI. There is no frontend and no CORS middleware (none is needed: nothing cross-origin calls the API yet).

> **Superseded handbook sections.** If a ThreadForge handbook exists under `docs/` (none is in this repository yet), these parts of it are replaced by the Google OIDC decision: email/password login, Argon2id password hashing, `POST /accounts`, `POST /sessions`, and operator password recovery. There are no passwords in this system. The handbook's other conventions are kept: UUID primary keys, UTC timestamps, `ON DELETE RESTRICT` foreign keys, a `version` column on mutable entity tables, and request IDs.

---

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (Docker Desktop on Windows/macOS). uv installs Python 3.12 for you (pinned in `backend/.python-version`).

```bash
# 1. Configuration (the .env file is git-ignored)
cp .env.example .env            # PowerShell: Copy-Item .env.example .env

# 2. Install dependencies (from the lockfile)
cd backend
uv sync

# 3. Start PostgreSQL 16 (data persists in a Docker volume; bound to 127.0.0.1:5442 only)
cd ..
docker compose up -d --wait

# 4. Apply migrations (schema comes ONLY from Alembic)
cd backend
uv run alembic upgrade head

# 5. Start the backend  (see "Run flags" below)
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

Check it: `curl http://127.0.0.1:8000/health/live` and `.../health/ready` both return `{"status":"ok"}`.
Without Google credentials the app starts and works; `/auth/login` answers `503 oidc_not_configured` until you configure them (below).

```bash
# Maintenance: delete expired login attempts and stale sessions (run from cron/scheduler)
uv run python -m app.commands.cleanup_auth          # --help for batch size / retention

# Tests and lint (tests create and use a separate database, threadforge_test)
uv run pytest
uv run ruff check .
uv run ruff format --check .

# Stop services
cd .. && docker compose down          # keeps the data volume
docker compose down -v                # also deletes the database volume
```

Notes for Windows: use **`127.0.0.1`**, not `localhost`, in `DATABASE_URL`. On some Windows machines `localhost` tries IPv6 first and every database connection pays a ~2 s timeout before falling back. The compose file publishes PostgreSQL on **5442** rather than 5432 so it does not collide with a PostgreSQL you may already have installed.

### Run flags
- `--no-access-log`: uvicorn's access log prints the full request line, **including the query string**, and `/auth/callback?code=…&state=…` carries the authorization code. The app logs its own access line (method, path, status, request ID — never the query).
- `--no-proxy-headers`: the app applies `X-Forwarded-*` itself, only from the addresses in `TRUSTED_PROXY_IPS` (empty = trust nobody). Leaving uvicorn's own handling on would trust `127.0.0.1` in addition.

---

## Google setup

1. Open the [Google Cloud Console](https://console.cloud.google.com/), create or select a project.
2. **APIs & Services → OAuth consent screen**: choose *External* (or *Internal* for a Workspace-only app). App name and support email are required. While the app is in **Testing** status, only the **test users** you list can sign in — add every Google account you want to try, including your second test account.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
4. Application type: **Web application**.
5. **Authorized redirect URIs**: add the exact callback — it must match `OIDC_CALLBACK_URL` character for character (scheme, host, port, path, no trailing slash):
   - development: `http://localhost:8000/auth/callback`
   - production: `https://<your-api-host>/auth/callback`
6. Copy the **Client ID** and **Client secret** into `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. Never commit them.
7. Open the app in the browser as `http://localhost:8000` (not `127.0.0.1`) so the host matches the redirect URI and the cookies' host.

Production requires HTTPS everywhere: `PUBLIC_BASE_URL`, `OIDC_CALLBACK_URL`, the frontend URLs and every trusted origin. Google will only accept plain-`http` redirect URIs for `localhost`. Production cookies are `Secure` and `__Host-`-prefixed, which browsers refuse to set over HTTP.

---

## Configuration

All settings are environment variables (or `.env` at the repo root). Invalid settings fail at startup with a message naming the setting; messages never include secret values. `.env.example` has the full list; the important ones:

| Variable | Meaning |
|---|---|
| `APP_ENV` | `development`, `test` or `production`. Production enforces everything in the next rows. |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:port/db` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Required in production. In development, unset/placeholder means "login not configured". |
| `GOOGLE_ISSUER`, `GOOGLE_DISCOVERY_URL` | Default to Google's. Always https. |
| `PUBLIC_BASE_URL` | Origin users reach the API on. |
| `OIDC_CALLBACK_URL` | Exact registered redirect URI; path must be `/auth/callback`, same origin as `PUBLIC_BASE_URL`. Built from settings only — never from the request's `Host` header. |
| `FRONTEND_SUCCESS_URL`, `FRONTEND_ERROR_URL` | Fixed redirect targets after login. Dev default: `<PUBLIC_BASE_URL>/auth/me`. **Production must set both.** |
| `TRUSTED_ORIGINS` | Comma-separated exact origins allowed in the `Origin` header of state-changing requests. Production must set it; no wildcards. |
| `SESSION_LIFETIME_SECONDS` | Absolute session lifetime, default 604800 (7 days). |
| `LOGIN_ATTEMPT_LIFETIME_SECONDS` | Default 600. |
| `MAX_PENDING_LOGIN_ATTEMPTS` | Global cap on unexpired attempts, default 1000. |
| `CSRF_SECRET` | HMAC key for CSRF tokens. Production: ≥ 32 chars, no placeholder text. |
| `TRUSTED_PROXY_IPS` | IPs/CIDRs whose `X-Forwarded-*` headers are trusted. Default: none. |
| `COOKIE_SECURE` | Dev only (default false). Production always forces `Secure`; `false` there is a startup error. |

Dev defaults allow plain `http` only for `localhost`/`127.0.0.1`. Nothing silently enables fake authentication: there is no bypass, fake provider, or impersonation endpoint.

---

## API

No route prefix (the repository had none). Every `/auth/*` response carries `Cache-Control: no-store`; every response carries `X-Request-ID`.

| Method | Route | Contract |
|---|---|---|
| GET | `/health/live` | `200` if the process responds. Never touches PostgreSQL. |
| GET | `/health/ready` | `200` if `SELECT 1` succeeds within `readiness_timeout_seconds` (2 s); else `503 database_unavailable`. Never calls Google. |
| GET | `/auth/login` | Stores a login attempt, sets the binding cookie, `303` to Google. `503 oidc_not_configured`, `503 provider_unavailable`, or `429 login_busy` (+`Retry-After`). |
| GET | `/auth/callback` | **Always a `303` redirect.** Success → `FRONTEND_SUCCESS_URL`. Failure → `FRONTEND_ERROR_URL?error=<code>`. |
| GET | `/auth/me` | `200 {id, display_name, email, email_verified}` or `401 unauthenticated`. |
| GET | `/auth/csrf` | `200 {"csrf_token": "..."}` for the current session, or `401`. |
| POST | `/auth/logout` | `204`. Needs `X-CSRF-Token` and a trusted `Origin` when a session exists (`403 csrf_failed` otherwise, session untouched). No valid session → `204` and the stale cookie is cleared. |

Error shape for all non-callback routes: `{"error": {"code": "...", "message": "...", "request_id": "..."}}`. Messages are fixed strings; exception text, SQL, secrets and provider responses are never returned.

**Callback error codes** (stable; in the `error` query parameter): `access_denied`, `invalid_login_attempt`, `provider_unavailable`, `invalid_token`, `account_disabled`, `login_failed`. No redirect target ever comes from a query parameter.

**`/auth/me` and account state.** A valid session whose user is `disabled` or `deleted` gets the *same* 401, code and message as no session, so the response never reveals account state. At login, such an account is redirected with `account_disabled` and no session is created. (Logout still works for such a session: it only ends it.)

### Database schema

UUID primary keys (`gen_random_uuid()`), `timestamptz` columns (connection pinned to UTC), all foreign keys `ON DELETE RESTRICT`, named CHECK constraints for states and lengths.

| Table | Purpose |
|---|---|
| `users` | The local account: `display_name`, `email` (profile only — **not unique, never used for lookup or authorization**), `email_verified`, `state` (`active`/`disabled`/`deleted`), `version`. No passwords. |
| `auth_identities` | `(issuer, subject)` **unique** → `user_id`; plus `provider_email(_verified)`. This is how a returning person is recognised. |
| `sessions` | `secret_hash` (SHA-256, unique), `expires_at`, `revoked_at`. The raw secret exists only in the browser's cookie. |
| `oidc_login_attempts` | Server-side state for an in-flight login: `state_hash`, `browser_binding_hash`, `nonce`, `pkce_verifier`, `expires_at`, `consumed_at`. |

---

## How it works (reading guide)

Suggested reading order: `app/config.py` → `app/main.py` → `app/middleware.py` + `app/errors.py` → `app/models/` + `migrations/versions/0001_auth_foundation.py` → `app/services/login_attempts.py` → `app/services/oidc.py` → `app/services/accounts.py` → `app/services/sessions.py` → `app/dependencies/auth.py` → `app/services/csrf.py` → `app/api/auth.py`.

**Login, click to cookie**

1. Browser → `GET /auth/login`. We fetch Google's discovery metadata (lazy, cached), generate four random values (`state`, binding secret, `nonce`, PKCE verifier), delete a bounded batch of expired attempts, check the pending-attempt cap under an advisory lock, insert the attempt (only *hashes* of state and binding secret), commit, set the HttpOnly binding cookie, and `303` to Google with `state`, `nonce` and an S256 `code_challenge`.
2. Google authenticates the user and redirects to `/auth/callback?code=…&state=…`.
3. We reject early on a provider `error` or malformed/missing/duplicated `state`/`code`/binding cookie.
4. One atomic `UPDATE … SET consumed_at = now() WHERE state_hash=… AND browser_binding_hash=… AND consumed_at IS NULL AND expires_at > now() RETURNING …` consumes the attempt. Two concurrent callbacks cannot both win. Committed. *No database transaction is open from here on.*
5. We exchange the code with Authlib (`fetch_token` with `code_verifier`), then validate the ID token with joserfc: signature against Google's JWKS, algorithm allow-list `RS256` (rejects `none` and `HS256` key confusion), issuer, audience, expiry/issued-at, plus our checks for `nonce`, `azp`, and a strictly boolean `email_verified`.
6. Resolve the account by `(issuer, subject)` — one transaction creates user + identity, or (on the unique-violation of a concurrent first login) rolls back, re-selects, and returns the winner. Returning logins refresh only `auth_identities.provider_email*`; `users.display_name` and `users.email` are set once at creation.
7. Create a fresh session and revoke the browser's previous valid one — one transaction. Clear the binding cookie, set the session cookie, redirect to the success URL.

**Why each secret exists**

| | Job |
|---|---|
| `state` | Ties the callback to an attempt *we* created (forged callbacks fail). |
| Browser-binding cookie | Ties that attempt to the browser that started it. `state` alone does not: it travels in URLs, so anyone who obtains a valid callback URL could finish the login in a different browser. The binding secret never leaves the originating browser's HttpOnly cookie. |
| `nonce` | Ties the *ID token* to this attempt (a token minted for another login is rejected). |
| PKCE verifier | Ties the *code* to a secret only we hold, so a stolen authorization code cannot be redeemed. |
| CSRF token | Protects authenticated state-changing requests (cookies are sent automatically, even by hostile pages). |

**OIDC identifies; our session maintains.** Google tells us once, at login, who the person is. After that the browser holds a random 256-bit secret in an HttpOnly cookie and PostgreSQL holds only its SHA-256, so a database leak or backup yields no usable cookies, and revocation is a row update. Accounts are keyed by the provider's stable `sub`, never email — emails change, get recycled, and may be unverified, so matching on them would allow account takeover.

**Cookies.** Session and binding cookies: `HttpOnly`, `Path=/`, `SameSite=Lax`, no `Domain`; `Secure` and the `__Host-` name prefix in production (the browser then enforces Secure + `Path=/` + no Domain). Lax rather than Strict is required: the binding cookie must be sent when Google redirects back (a cross-site navigation). Deletions repeat the same attributes. Nothing is stored in `localStorage`.

**CSRF, and how future mutating routes use it.** `token = HMAC-SHA256(CSRF_SECRET, "csrf-v1:" + session secret)`. A mutating request must carry the session cookie, an `X-CSRF-Token` header equal to that value (constant-time compare), and an `Origin` header exactly matching `TRUSTED_ORIGINS`. A missing `Origin` is rejected (this API is browser-only in this part). CORS and SameSite are not relied on. A frontend calls `GET /auth/csrf` after login, keeps the token **in memory**, and sends it as `X-CSRF-Token` on every `POST/PUT/PATCH/DELETE`; if it gets `403 csrf_failed` it fetches a fresh one. A route enforces it with `enforce_csrf(request, settings, auth_context)` from `app/services/csrf.py`.

**Authentication vs authorization.** This part establishes *who the user is*. Part 2 will check *what that user can access*: every future room route will declare `user: CurrentUser` (`app/dependencies/auth.py`) first, then check membership. Authorization can never run without an authenticated user.

**Async.** Later parts add WebSockets, Pub/Sub and a dispatcher — long-lived, I/O-bound work sharing one event loop. Starting with an async stack (FastAPI, SQLAlchemy asyncio, asyncpg, httpx) avoids retrofitting every query site later. Rule: one `AsyncSession` per request or worker operation, never shared across concurrent tasks.

### Proxies and logs
- Behind a reverse proxy set `TRUSTED_PROXY_IPS` to the proxy's address(es). `X-Forwarded-For`/`-Proto` from any other peer is ignored.
- **Reverse-proxy access logs** (nginx, ALB, Cloudflare, …) usually log the full URL including query strings. Application code cannot prevent that. Configure the proxy to omit or redact the query string for `/auth/callback` (for example log `$uri` instead of `$request_uri` in nginx), and restrict access to those logs.
- Application logs never contain `state`, `nonce`, the PKCE verifier, the binding secret, the authorization code, tokens, session secrets, or query strings. A test scans captured logs during a full login to enforce this.

---

## Manual verification checklist (real Google)

Automated tests stub Google at the HTTP level; **they do not prove the real Google integration works.** Do this once with real credentials. Mark each step.

- [ ] `docker compose up -d --wait`, then `cd backend && uv sync && uv run alembic upgrade head`.
- [ ] Create Google credentials (see *Google setup*), put them in `.env`, add yourself (and a second account) as test users.
- [ ] Start the backend (command above). `curl http://127.0.0.1:8000/health/ready` → `ok`.
- [ ] In a browser open `http://localhost:8000/auth/me` → `401 unauthenticated` JSON.
- [ ] Open `http://localhost:8000/auth/login`. Sign in with Google. You land on `/auth/me` showing `{id, display_name, email, email_verified}` for your account.
- [ ] DevTools → Application → Cookies: `tf_session` is `HttpOnly`, `SameSite=Lax`, `Path=/`; `tf_login_binding` is gone.
- [ ] Inspect the database **without exposing secrets**:
  ```bash
  docker exec threadforge-postgres-1 psql -U threadforge -d threadforge -c \
    "select id, display_name, email_verified, state, version from users;" -c \
    "select id, user_id, issuer, subject, provider_email_verified from auth_identities;" -c \
    "select id, user_id, left(secret_hash, 8) as hash_prefix, created_at, expires_at, revoked_at from sessions;" -c \
    "select id, created_at, expires_at, consumed_at from oidc_login_attempts;"
  ```
  The `sessions` row shows a hash prefix, not your cookie value. (Avoid selecting `nonce`/`pkce_verifier`/hash columns in full.)
- [ ] In the DevTools console on `http://localhost:8000/auth/me`, log out:
  ```js
  const {csrf_token} = await (await fetch('/auth/csrf')).json();
  const r = await fetch('/auth/logout', {method: 'POST', headers: {'X-CSRF-Token': csrf_token}});
  r.status  // 204
  ```
- [ ] Reload `/auth/me` → `401`. The `sessions` row now has `revoked_at` set.
- [ ] Copy your old `tf_session` value *before* logging out in a second run, restore it via DevTools after, and confirm `/auth/me` is still `401` (reuse of the old cookie fails).
- [ ] Try logout without the header: `fetch('/auth/logout', {method:'POST'})` while logged in → `403 csrf_failed`, and you are still logged in.
- [ ] Click *Cancel* on Google's consent screen → you land on `/auth/me?error=access_denied` and no session exists.
- [ ] Repeat the full sign-in/out with a **second Google account**: a second `users` row and `auth_identities` row appear; the first account is unaffected.
- [ ] Sign in twice in a row from the same browser: the first session shows `revoked_at` set, the second is live.

Status of this checklist for the current commit: **pending** (no Google credentials were available when Part 1 was implemented).

---

## Known limitations

- **No per-IP rate limiting.** `/auth/login` writes a row for any caller. Bounded by opportunistic cleanup and the global `MAX_PENDING_LOGIN_ATTEMPTS` cap (`429 login_busy`), but one client can fill the cap and temporarily block others. Per-IP limiting belongs at the proxy/WAF or in a later part.
- **One active login attempt per browser.** Starting a login overwrites the binding cookie and invalidates the earlier attempt (a second tab's login must be restarted).
- **No sliding sessions, no last-seen tracking.** Sessions have an absolute 7-day lifetime.
- Logout requires an `Origin` header. Non-browser clients would need to send one.
- Google metadata and signing keys are cached in process memory (1 h; keys are refetched at most every 30 s on an unknown key ID). Each API process fetches its own copy.
- Cleanup is a command to be scheduled externally; there is no built-in scheduler.
- The pending-attempt cap counts all unexpired, unconsumed attempts globally.

## Project layout

```
backend/app/        main.py config.py db.py middleware.py errors.py logging_config.py
  api/              health.py auth.py
  services/         oidc.py login_attempts.py accounts.py sessions.py csrf.py cookies.py security.py
  dependencies/     auth.py
  models/ schemas/  commands/cleanup_auth.py
backend/migrations/ Alembic (async template), versions/0001_auth_foundation.py
backend/tests/      pytest suite (fake Google in tests/fake_google.py)
compose.yaml        PostgreSQL 16
.env.example
```
