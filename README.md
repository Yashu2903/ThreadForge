# ThreadForge
A collaborative AI workspace where teams turn conversations and source documents into cited drafts, edit together in real time, review AI revisions, and approve and export versioned documents.

## Status: Part 6 in progress (documents, checkpoint 2 of 9); Parts 1 to 5 complete: authentication, rooms, messages, durable events, reconnect recovery, presence and live access

Part 1 (backend foundation and authentication): FastAPI app, PostgreSQL + Alembic, Google OpenID Connect login, local users and provider identities, PostgreSQL-backed browser sessions, CSRF-protected logout, health endpoints, request IDs, a cleanup command.

Part 2 (rooms, invitations, permissions): private rooms with an owner, editors and viewers; single-use invitation links; membership management; server-side authorization on every route. See [Part 2](#part-2-rooms-invitations-and-authorization).

Part 3 (persistent room messages over HTTP): send, history, single lookup, duplicate-send protection, edit with conflict protection, soft delete, a PostgreSQL write rate limit. See [Part 3](#part-3-persistent-room-messages-over-http). 

Part 4 (durable room events and reliable publication of live updates): every supported change also records a room event and a transactional outbox row; a standalone dispatcher publishes through Redis Pub/Sub; an authenticated room WebSocket delivers events across several API processes; a protected HTTP endpoint serves event history. See [Part 4](#part-4-durable-room-events-and-reliable-publication-of-live-updates). **Live delivery is best-effort, at-least-once publication with possible loss and duplicates; PostgreSQL history is the source of truth.**

Part 5 (reconnect recovery, presence, typing, live access enforcement): a consistent bootstrap (`GET /rooms/{id}/sync`), ordered replay from a client cursor, a safe switch to live delivery where Redis only *hints* and PostgreSQL is always read in order, acknowledgements, lost-hint detection by reconciliation, Redis-only presence and typing, access checked for the whole life of every connection, and a TypeScript client (`client/`). See [Part 5](#part-5-reconnect-recovery-presence-typing-indicators-and-live-access-enforcement). **Still never exactly-once or guaranteed delivery.**

Not implemented (out of scope): documents, uploads, AI, event pruning. There is no frontend and no CORS middleware (none is needed: nothing cross-origin calls the API yet).

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

# 3. Start PostgreSQL 16 and Redis 7 (Postgres on 127.0.0.1:5442, Redis on 127.0.0.1:6390; data in a Docker volume)
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

Part 4 settings (Redis, outbox dispatcher, WebSockets) are listed in [Part 4](#configuration-added-in-part-4). Part 3 settings: `MESSAGE_FINGERPRINT_SECRET` (**required**; HMAC key for the duplicate-send fingerprint; production: ≥ 32 chars, no placeholder, validated like `CSRF_SECRET`) and `MESSAGE_WRITES_PER_MINUTE` (default 30; see Part 3).

Part 2 settings (all validated at startup; placeholders in `.env.example`):

| Variable | Meaning |
|---|---|
| `FRONTEND_BASE_URL` | Origin of the frontend; invitation links are `{FRONTEND_BASE_URL}/join#token=<token>`. Dev default: the API origin. **Production must set it, https.** No query/fragment; a trailing `/` is stripped. |
| `INVITATION_DEFAULT_HOURS` / `_MIN_HOURS` / `_MAX_HOURS` | Lifetime default 24, bounds 1–168. Must satisfy min ≤ default ≤ max. |
| `MAX_OWNED_ROOMS_PER_USER` | Default 20. |
| `MAX_MEMBERS_PER_ROOM` | Default 50 active members. |
| `MAX_ACTIVE_INVITATIONS_PER_ROOM` | Default 50 (unused, unrevoked, unexpired). |

Dev defaults allow plain `http` only for `localhost`/`127.0.0.1`. Nothing silently enables fake authentication: there is no bypass, fake provider, or impersonation endpoint.

---

## API

No route prefix (the repository had none). Every response except `/health/*` carries `Cache-Control: no-store` (sessions, room data and invitation tokens are private); every response carries `X-Request-ID`. Part 2 routes are documented in [Part 2](#part-2-rooms-invitations-and-authorization).

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

## Part 2: rooms, invitations and authorization

### Product rules
Rooms (no workspace/channel hierarchy). A room has one **owner**, zero or more **editors** and **viewers**; creating a room creates the owner membership. Joining is **only** through an invitation link or key; rooms are not discoverable. An invitation grants one role (editor or viewer — never owner) in one room, expires, is revocable, and is redeemed once. It is a **bearer credential**: whoever holds the token and is authenticated can redeem it. It is not tied to an email address and nothing is emailed. Not implemented (deliberately deferred): room closing/archiving/deletion, ownership transfer, member self-leave.

### Routes and permissions
Every route needs authentication (401 `unauthenticated`); every mutation also needs CSRF (`X-CSRF-Token` + trusted `Origin`, 403 `csrf_failed`).

| Method | Route | Owner | Editor | Viewer | Nonmember |
|---|---|---|---|---|---|
| POST | `/rooms` (body: `name`) → 201 | any authenticated user | | | |
| GET | `/rooms?limit&cursor` → caller's **active** rooms | ✓ | ✓ | ✓ | (not listed) |
| GET | `/rooms/{id}` | ✓ | ✓ | ✓ | 404 |
| PATCH | `/rooms/{id}` (only `name`) | ✓ | 403 | 403 | 404 |
| GET | `/rooms/{id}/members?limit&cursor` | ✓ | ✓ | ✓ | 404 |
| PATCH | `/rooms/{id}/members/{user_id}` (`role`: editor\|viewer) | ✓ | 403 | 403 | 404 |
| DELETE | `/rooms/{id}/members/{user_id}` → 204 | ✓ | 403 | 403 | 404 |
| POST | `/rooms/{id}/invitations` (`role`, `expires_in_hours`) → 201 + raw token **once** | ✓ | 403 | 403 | 404 |
| GET | `/rooms/{id}/invitations?limit&cursor` (metadata + status) | ✓ | 403 | 403 | 404 |
| DELETE | `/rooms/{id}/invitations/{invitation_id}` → 204 | ✓ | 403 | 403 | 404 |
| POST | `/invitations/redeem` (body: `token`) → 200 | any authenticated user | | | |

"Nonmember" includes a **removed** member: removed memberships grant nothing. Authorization failures for nonmembers are always `404 room_not_found` (identical to an unknown or malformed room id), so a private room's existence and name are never revealed. The owner cannot remove or demote themselves. No endpoint accepts `owner_id`, an actor id, or an owner role; request schemas reject unknown fields (422).

Invitation links look like `{FRONTEND_BASE_URL}/join#token=<token>`. The token is in the URL **fragment**, which browsers never send to servers, so it stays out of access logs and `Referer` headers. A GET to a link never joins anyone: joining is the authenticated, CSRF-protected `POST /invitations/redeem`. The future join page must read the fragment, POST it, and remove it from the address bar (`history.replaceState`). No frontend exists yet.

**Order of checks** for every room route: authentication (401) → CSRF on unsafe methods (403) → room authorization (404/403) → request-body validation (422) → service, which re-checks authorization under a lock. FastAPI resolves dependencies before it raises body-validation errors, so a nonmember sending an invalid body gets 404, never 422 (tested). Documented deviations: path ids that are not valid UUIDs are treated as "not found" (404), never 422; `expires_in_hours` bounds come from settings and are checked after authorization; `limit` outside 1–100 is `validation_error`.

**Pagination.** `limit` default 20, max 100. `next_cursor` is an opaque base64url of the last row's `(timestamp, id)` at microsecond precision (so rows sharing a timestamp are neither skipped nor repeated). Rooms and invitations: newest first. Members: `joined_at ASC, user_id ASC`. Anything malformed → `422 invalid_cursor`.

### Error codes (stable, lowercase)

| Code | Status | Meaning |
|---|---|---|
| `unauthenticated` | 401 | No valid session (also for disabled/deleted accounts) |
| `csrf_failed` | 403 | Missing/invalid CSRF token or untrusted/missing Origin |
| `room_not_found` | 404 | Room missing **or the caller is not an active member** |
| `insufficient_role` | 403 | Active member lacks the owner role |
| `member_not_found` | 404 | Target is not an active member of this room (PATCH), or never existed (DELETE) |
| `invitation_not_found` | 404 | Invitation id not in this room |
| `invitation_invalid` | 404 | **Every** redemption failure: unknown, malformed, expired, revoked, or used by someone else. One status, code and message |
| `invitation_already_redeemed` | 409 | Owner tried to revoke a used invitation |
| `owner_protected` | 409 | Attempt to remove or demote the owner (including self) |
| `room_limit_reached` / `member_limit_reached` / `invitation_limit_reached` | 409 | Caps (enforced under locks) |
| `invalid_cursor` | 422 | Bad pagination cursor |
| `validation_error` | 422 | Bad body structure or values |

### Schema (migration `0002`)
`rooms` (name 1–120 trimmed; `owner_id`; `membership_epoch >= 1` starting at 1; `version`), `memberships` (PK `(room_id, user_id)`; role owner/editor/viewer; state active/removed; `removed_at`), `invitations` (SHA-256 `token_hash` unique; role editor/viewer; `expires_at`, `revoked_at`, `used_at`/`used_by`). The database enforces by itself: **at most one active owner per room** (partial unique index `WHERE role='owner' AND state='active'`), an **owner row is never marked removed**, `removed_at` matches `state`, `used_at`/`used_by` are both null or both set, `expires_at > created_at`, and every foreign key is `ON DELETE RESTRICT`. "Exactly one owner row matching `rooms.owner_id`" is enforced by the room-creation transaction and the mutation rules (the owner can never be removed, demoted, or granted by invitation), not by a constraint. Removed rows are retained and **reactivated** (same row, `joined_at` reset) on a legitimate rejoin. Deliberately no CHECKs between timestamps written by different transactions: database clocks are not monotonic across backends.

`membership_epoch` is an access-policy version for future live connections and caches. It increments by exactly 1 (under the room lock) only when access actually changes: a membership is activated or reactivated, a member is removed, or a member's role changes. It does **not** change on room creation (epoch 1 already represents the owner-only state), no-op role updates, renames, invitation creation/revocation, or an idempotent redemption. `version` follows the Part 1 convention: it increments on every real `UPDATE` of the row (a rename, or an epoch bump); a same-name rename is a no-op. If-Match enforcement is deferred.

### Transactions and locks
**One pattern:** every unit of database work is a service function (`app/services/rooms.py`, `memberships.py`, `invitations.py`) that takes the **sessionmaker**, never an existing session, opens its own session and `begin()`s one transaction, which commits at block exit. Dependencies (authentication, CSRF, fast pre-checks) use their own short sessions that are closed before the service runs, so no transaction is ever already open and `begin()` can never collide with SQLAlchemy's autobegin. Isolation is PostgreSQL's default `READ COMMITTED`. There are no network calls inside transactions and no permission cache: authoritative state is read from PostgreSQL on every request.

**Lock order (always):**
1. the **room** row — `SELECT … FOR NO KEY UPDATE`
2. the **invitation** row — `FOR UPDATE` (redeem, revoke)
3. **membership** row(s) — `FOR UPDATE` (role change, remove, redeem)

(Room creation first locks the creator's `users` row `FOR NO KEY UPDATE` to serialize one user's creations for the owned-rooms cap; nothing else locks user rows, so it cannot form a cycle.) Mutating services take the room lock **first**, then read the actor's membership and role inside the same transaction; a read before the lock is never trusted. Dependencies may pre-check for fast rejection, but a pre-check is never the authority for a mutation (tested with a deliberately stale pre-check).

**Why `FOR NO KEY UPDATE`, not `FOR UPDATE`:** inserting a row into a table with a foreign key to `rooms` (a membership, an invitation) takes a weak `FOR KEY SHARE` lock on the room row. `FOR UPDATE` conflicts with `FOR KEY SHARE`, so it would make those inserts wait; `FOR NO KEY UPDATE` does not conflict with `FOR KEY SHARE` but does conflict with itself, so room mutators still exclude one another. I checked this against the PostgreSQL 16 documentation ("Explicit Locking", the table of conflicting row-level lock modes). The docs page does not state which lock a foreign-key insert takes, so `tests/test_lock_modes.py` establishes it empirically (an FK insert proceeds while the room is locked `FOR NO KEY UPDATE`, and is blocked under `FOR UPDATE`).

Removal and role changes `UPDATE` the membership row (never delete it), so a future `SELECT … FOR SHARE` membership check on a message send conflicts with them. All connections have a `lock_timeout` (10 s; 3 s in tests) so a lock bug fails fast instead of hanging.

### Invitation redemption (one transaction)
An unlocked lookup by token hash finds the room to lock; everything is revalidated after the locks. With U the caller, I the invitation, M the caller's membership row: (1) no match / malformed token → `invitation_invalid`; (2) `I.used_by == U`: M active → return it unchanged (a retry, even if I has since expired), M removed → invalid; (3) used by someone else → invalid; (4) revoked or `expires_at <= now()` (database clock) → invalid; (5) M active (any role, even owner) → return it, **do not** consume the invitation, change the role or touch the epoch; (6) otherwise check the member cap (`member_limit_reached`, invitation untouched), create or reactivate M with I's role, consume I, bump the epoch — all in one commit. Two requests for one invitation: exactly one consumption. Same user twice (double-click): both 200 with the same membership. Different users: exactly one wins, the other gets `invitation_invalid`.

### Logging
Membership and room changes emit one structured line after commit: `action=<…> room_id=<…> actor_id=<…> target_id=<…> request_id=<…>` (actions: `room_created`, `room_renamed`, `member_joined`, `member_rejoined`, `member_role_changed`, `member_removed`, `invitation_created`, `invitation_revoked`). Tokens, token hashes, cookies and redemption request bodies are never logged (tested).

### Where later parts hook in
- **Part 3 (messages) — now implemented** in `app/services/messages.py`. It holds the same room lock (`FOR NO KEY UPDATE`) that every membership mutation takes, so a message write and a removal or role change serialize, and no extra `FOR SHARE` on the membership row is needed (the originally planned design). See [Part 3](#part-3-persistent-room-messages-over-http).
- **Part 4 (room events, outbox) — now implemented.** `record_room_event()` (`app/services/room_events.py`) is called inside the transactions of `rooms.create_room`, `rooms.rename_room`, `invitations.redeem_invitation`, `memberships.change_role`, `memberships.remove_member` and the three message writes, always while holding the room lock. The `membership_epoch` plan above was replaced by the per-room `event_seq`. See [Part 4](#part-4-durable-room-events-and-reliable-publication-of-live-updates).
- **Part 5 (revocation):** removal currently blocks subsequent **HTTP** requests. WebSocket/connection revocation is not implemented and not claimed.

### Manual verification (Part 2, real Google accounts)
**Status: pending** — no Google credentials were available; the automated test `tests/test_manual_sequence.py` runs the same script (below) against fixture users, which proves the script and the flow but not the real Google login.

1. Complete the Part 1 setup and sign in as account **A** and as account **B** through the real Google flow, in two separate browser profiles.
2. Copy each `tf_session` cookie **value** (DevTools → Application → Cookies) into environment variables without putting it on a command line or into a transcript. PowerShell: `$env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput` (same for `TF_COOKIE_B`).
3. From `backend/`: `uv run python -m scripts.manual_part2_check`. Add `$env:TF_INSPECT_DB = "1"` to also print the room, membership and invitation rows after each step (invitations show only a hash prefix, never a token).

The script: A creates a room → A creates an editor invitation → B cannot read the room (404) → B redeems → B reads the room and members → B cannot invite or remove (403) → A changes B to viewer → A removes B → B's access is gone (404) → the used invitation cannot restore access → A creates a fresh invitation and B rejoins. It checks `membership_epoch` after every step (expected 1, 1, 1, 2, 2, 2, 3, 4, 4, 4, 5) and never prints cookies or tokens. In production set `TF_COOKIE_NAME=__Host-tf_session`, `TF_BASE_URL` and `TF_ORIGIN`.

### Part 2 limitations
- **No rate limiting** on `POST /invitations/redeem` or invitation/room creation (the redeem token is 256 random bits, so guessing is infeasible, but flooding is not throttled).
- **No `Idempotency-Key`** on `POST /rooms` and `POST …/invitations`: a retried create makes a duplicate (a retried *redeem* is safe by design).
- **No audit table:** membership changes are only in structured application logs.
- **No `If-Match`** enforcement: `version` is maintained but not yet checked.
- Removal blocks HTTP access only (no live-connection revocation until Part 5).
- Invitations are not bound to an email address or account; whoever holds the token and is signed in can use it once.
- Room names are not unique and are visible to every member.
- **Part 2 routes that take a JSON body** (`POST /rooms`, `PATCH /rooms/{id}`, `POST …/invitations`, `POST /invitations/redeem`, `PATCH …/members/{id}`) let FastAPI parse the body before the authentication dependency runs, so an unauthenticated or unauthorized caller sending *syntactically malformed* JSON gets 422 instead of 401/404. It reveals nothing, and Part 3's message routes avoid it (they parse the body themselves after the gates), but the Part 2 routes were not changed.

---

## Part 3: persistent room messages over HTTP

Plain-text messages in a room: send, read paginated history, read one, edit, delete. **Nothing is live**: no WebSockets, Redis, room events or outbox. A client sees another client's change only by fetching over HTTP.

### Product rules
- A message belongs to exactly one room and one author; the author is always the authenticated session user (no request field can name one).
- **Chat is open to every active member, whatever their role.** Owners, editors and viewers can all read, send, edit their own messages and delete their own messages. Only the **author** can edit, and the **owner can delete anyone's** message; every other member can delete only their own. The owner **cannot edit** another person's message. Role restrictions (viewers cannot edit documents or upload sources) apply only to **documents and source uploads in later parts**; chat depends only on being an active member. A role change (editor ↔ viewer) never changes what a user can do with messages; **removal** does.
- Removed members cannot read or modify room messages.
- A deleted message becomes a **tombstone** (body `NULL`) and can never be edited or restored.
- **History visibility:** every active member reads the room's *complete* history, including messages sent before they joined or while they were removed. There is no per-member visibility window. Messages written by since-removed members remain visible.
- No attachments, reactions, threads, Markdown rendering or read receipts. Content is untrusted plain text: it is never interpreted or rendered as HTML.

### Routes and permissions
Every route needs authentication (401); every mutation also needs CSRF (403 `csrf_failed`). All message responses, success and error, carry `Cache-Control: no-store`.

| Method | Route | Owner | Editor | Viewer | Nonmember / removed |
|---|---|---|---|---|---|
| POST | `/rooms/{id}/messages` — body `{client_message_id, body}` → **201** new, **200** matching retry | ✓ | ✓ | ✓ | 404 `room_not_found` |
| GET | `/rooms/{id}/messages?limit=50&before=<cursor>` | ✓ | ✓ | ✓ | 404 |
| GET | `/rooms/{id}/messages/{message_id}` | ✓ | ✓ | ✓ | 404 |
| PATCH | `/rooms/{id}/messages/{message_id}` — body `{body, expected_version}` | own messages only | own messages only | own messages only | 404 |
| DELETE | `/rooms/{id}/messages/{message_id}?expected_version=N` → 204 | **any** message | own messages only | own messages only | 404 |

Permission summary (a "403" below is always `message_not_author`; **no message route returns `insufficient_role`**):

| Role | Read | Send | Edit own | Delete own | Edit others' | Delete others' |
|---|---|---|---|---|---|---|
| Owner | ✓ | ✓ | ✓ | ✓ | 403 | ✓ |
| Editor | ✓ | ✓ | ✓ | ✓ | 403 | 403 |
| Viewer | ✓ | ✓ | ✓ | ✓ | 403 | 403 |
| Removed member / nonmember | 404 | 404 | 404 | 404 | 404 | 404 |

**Gate order** for every message route: (1) body-size middleware → 413; (2) authentication → 401; (3) CSRF on unsafe methods → 403; (4) an **unlocked** membership pre-check → 404 for a nonmember or removed user; any *active* member passes whatever their role (people with no access can never take the room lock; the pre-check is never the authority); (5) request validation → 422; (6) the transaction: lock the room, **recheck active membership** inside it (never role), then write. Message routes read and validate the JSON body themselves after steps 2–4 (FastAPI would otherwise parse it before authentication runs), so the order holds even for malformed JSON.

### Message representation
One shape everywhere: `id, room_id, author_id, author_display_name, client_message_id, body, created_at, edited_at, deleted_at, deleted_by, version, is_deleted`. Tombstones have `body: null`; deleted text is never returned. `author_display_name` is read from `users.display_name` at query time (not copied into message rows), or `"Deleted user"` when the user is deleted. No emails, provider identity, session data, fingerprint or exception text.

### Schema (migration `0003`)
`messages`: `id`, `room_id`, `author_id` (all foreign keys `RESTRICT`, nothing cascades, so removing a member never removes messages), `client_message_id`, `body` (null only after deletion), `initial_request_fingerprint` (32 bytes, never exposed), `created_at`, `edited_at`, `deleted_at`, `deleted_by`, `version`. Named constraints: `uq_messages_room_author_client` (unique `(room_id, author_id, client_message_id)`), `ck_messages_body_matches_deletion` (live ⇒ body of 1–5000 characters; deleted ⇒ body null), `ck_messages_deleted_pair`, `ck_messages_version_positive`, `ck_messages_fingerprint_length`. One index, `(room_id, created_at, id)`, serves history. `room_id`, `author_id`, `client_message_id`, `initial_request_fingerprint` and `created_at` are **immutable after insert**, enforced by a database trigger (`trg_messages_immutable`), not just by convention.

`version` **starts at 1**, unlike the default-0 `version` columns of Parts 1 and 2, because clients echo it back as `expected_version`; 1 means "never edited". `message_write_buckets` holds the rate-limit counters (below).

**Timestamps use `clock_timestamp()`, never `now()`.** `now()` is the time the *transaction started*. A transaction that started earlier but then waited for the room lock would stamp an *older* time than a message that was already committed, so a client paginating by `(created_at, id)` could skip it. `created_at`, `edited_at` and `deleted_at` are therefore assigned in SQL with `clock_timestamp()` *after* the room lock is held (the column deliberately has no default). Writes in a room are serialized by that lock, so the timestamps are non-decreasing in commit order. Residual limits: equal timestamps are ordered by `id`, and a backwards system-clock adjustment can still reorder (the database clock is not guaranteed monotonic).

### Validation (no normalization)
Requests are strict and reject unknown fields: send takes exactly `client_message_id` (a UUID string; case-insensitive, normalized) and `body` (a JSON string, no coercion); edit takes exactly `body` and `expected_version` (a JSON integer ≥ 1: strings, floats and booleans are rejected, as are values above bigint). Body rules: **1–5,000 Unicode code points**; rejected: NUL, lone surrogates (category `Cs`), control characters (`Cc`) other than tab/LF/CR (these would otherwise fail inside PostgreSQL or the UTF-8 encoder and become a 500); the body must contain at least one *visible* character (neither `str.isspace` nor category `Cf`, so zero-width-only bodies are rejected). **No normalization of any kind is applied**: the stored body is exactly what was validated and submitted (leading/trailing whitespace and line breaks included). Validation errors never echo the input (Pydantic's `input` field is not returned or logged). **Request size:** an ASGI middleware enforces 128 KiB, counting bytes as they stream in (an oversized `Content-Length` is rejected early, but it is never trusted alone: it may be missing or false). 5,000 characters fully JSON-escaped is at most about 60 KB, so 128 KiB is generous headroom.

### Sending and duplicate protection
The client generates `client_message_id` once per new send and **reuses it on retries**; an intentional repeat of the same text gets a new id. Under the room lock the server looks up `(room, author, client_message_id)`:
- not found → rate-limit, insert (version 1), commit, respond **201**;
- found with a matching fingerprint → respond **200** with the message's *current* representation (edited text after an edit, a tombstone after a deletion; a retry never restores text), with no write and no rate-limit use;
- found with a different fingerprint → **409 `message_id_reused`**.

Permission is checked *before* the existing row is returned, so a removed or downgraded user cannot use a retry to bypass current restrictions. Deduplication is by `client_message_id` only, never by matching text. If an `INSERT` ever raises a unique violation on `uq_messages_room_author_client` (it cannot while the room lock serializes writers; the constraint is the final safeguard) the transaction is rolled back and re-run once, answering as a retry, never a 500 (tested by forcing two sends past the lookup with a barrier).

**The fingerprint is keyed.** `initial_request_fingerprint = HMAC-SHA256(MESSAGE_FINGERPRINT_SECRET, "msg-create-v1\n" + canonical JSON of {client_message_id, body})`, compared with `hmac.compare_digest`. A plain unsalted hash of a short message can be reversed by guessing candidate texts, which would defeat deletion (the row would keep an offline-checkable copy of the text); an HMAC cannot be tested without the key. Retaining a keyed fingerprint is not the same as retaining the text. **Rotating the secret** makes in-flight retries fail with `message_id_reused`, which is acceptable because retries happen within seconds.

### History and pagination
`GET /rooms/{id}/messages`: `limit` default 50 (outside 1–100 → 422 `validation_error`), optional `before`. The first request returns the **newest** page; items *within* a page are in ascending chronological order. Older pages use keyset pagination: `WHERE room_id = :room AND (created_at, id) < (:ts, :id) ORDER BY created_at DESC, id DESC LIMIT :limit + 1`, which uses the `(room_id, created_at, id)` index and never `OFFSET`. The extra row reveals whether an older page exists without a count query; `next_before` is the cursor of the **oldest returned item** when one does, else `null`. One query joins `users` for the author name, so the number of statements does not grow with the number of messages (tested). Reads take **no locks**.

The cursor is an opaque URL-safe base64 of a versioned payload (`m1 | room_id | created_at with microseconds | id`). It is parsed strictly: anything malformed, non-canonical, of another version, or naming a different room is 422 `invalid_cursor`. It is **unsigned**: a cursor is a pagination position, never an authorization credential. Forging one can only move the position within a room the caller can already read; every request is authorized on its own. This is **history pagination, not a synchronization protocol**, and not a frozen snapshot: messages written between two requests appear on a later newest-page fetch, and edits and deletions show up on re-fetch.

### Editing and deleting (checks run in exactly this order, after the room lock)
**PATCH**: recheck active membership (the author must still be an active member, of any role: a viewer may edit their own messages) → load the message **scoped to the room** `FOR UPDATE`, else 404 `message_not_found` → not the author, 403 `message_not_author` → already deleted, 409 `message_deleted` → `expected_version` ≠ current, 409 `message_version_conflict` with `current_version` in the error body (a stale version conflicts *even if the text matches*) → body exactly equals the current body: 200, nothing changes (no version bump, no timestamp, no rate-limit use) → rate limit → update body, `edited_at = clock_timestamp()`, `version + 1`. There is no separate edit idempotency key in this part. Only the latest text is kept (no `message_versions` table).

**DELETE** (`expected_version` is a required query parameter, else 422): recheck active membership (any role) → load the message scoped to the room `FOR UPDATE`, else 404 → the owner may delete any, every other member only their own, else 403 `message_not_author` → **already deleted: 204, no change, no rate-limit use, regardless of `expected_version`** → stale `expected_version`, 409 `message_version_conflict` → rate limit → `body = NULL`, `deleted_at = clock_timestamp()`, `deleted_by = actor`, `version + 1`. The row is never physically deleted. Authorization comes before the already-deleted shortcut so that someone who may not touch a message learns nothing about its state.

Two concurrent edits with the same `expected_version`: exactly one succeeds and the other gets `message_version_conflict`. No silent last-write-wins.

### Locks and transactions
Same single pattern as Part 2: every operation is a service function in `app/services/messages.py` that takes the sessionmaker, opens its own session and `begin()`s one transaction, with **one commit point** per function (so Part 4 can add its inserts in the same transaction). Isolation stays at PostgreSQL's default `READ COMMITTED` (the in-lock lookups rely on each statement seeing the latest committed data) and **must not be raised**. The response is built only after the commit, from a fresh read. No network calls in transactions.

**Lock order:** (1) the room row `FOR NO KEY UPDATE`, (2) the message row `FOR UPDATE`, (3) the rate-limit bucket row. Every membership mutation also takes the room lock first, so holding it guarantees the caller's membership cannot change underneath the request; no extra `FOR SHARE` on the membership row is needed. **Trade-off:** all message writes in one room are serialized. At ThreadForge room sizes and write limits that is cheap, and Part 4's per-room event sequence counter needs this same lock anyway. A write committed before a removal is valid; a write ordered after it fails its in-transaction membership check (tested in both orders).

### Rate limit (PostgreSQL, no Redis)
Fixed window: `MESSAGE_WRITES_PER_MINUTE` (default 30) successful writes per user per room per minute, counting **new sends, edits that change content, and deletions of live messages** in one bucket for the acting user. **Not counted:** matching send retries, unchanged edits, repeated deletes, and any request that fails before the write. One atomic statement in the same transaction as the message change, using the *database* clock:

```sql
INSERT INTO message_write_buckets (room_id, user_id, window_start, count)
VALUES (:room, :user, date_trunc('minute', clock_timestamp()), 1)
ON CONFLICT (room_id, user_id, window_start)
DO UPDATE SET count = message_write_buckets.count + 1
WHERE message_write_buckets.count < :limit
RETURNING count;
```
No row returned means the window is full: the transaction rolls back and the response is **429 `rate_limited`** with an integer `Retry-After` (seconds until the next minute, minimum 1). A failed or rolled-back write consumes no capacity because the increment is in the same transaction. **Fixed-window caveat:** near a minute boundary a user can burst up to twice the limit. The cleanup command (`python -m app.commands.cleanup_auth`) also deletes bucket rows older than 24 hours in batches; it never deletes messages.

### Error codes

`insufficient_role` is **not** used by any message route (it remains for the Part 2 owner-only routes: invitations, role changes, removal, room rename).

| Code | Status | Meaning |
|---|---|---|
| `unauthenticated` | 401 | No valid session |
| `csrf_failed` | 403 | Missing/invalid CSRF token or untrusted Origin |
| `room_not_found` | 404 | Room missing, or the caller is not an active member |
| `message_not_found` | 404 | Message not in this room |
| `message_not_author` | 403 | Editing, or deleting, a message the caller may not |
| `message_id_reused` | 409 | Same `client_message_id`, different original content |
| `message_version_conflict` | 409 | Stale `expected_version` (error body includes `current_version`) |
| `message_deleted` | 409 | Edit attempted on a tombstone |
| `rate_limited` | 429 | Write limit reached (`Retry-After` set) |
| `payload_too_large` | 413 | Body exceeds 128 KiB |
| `invalid_cursor` | 422 | Bad pagination cursor |
| `validation_error` | 422 | Bad fields, content, or limit |

Message bodies, cookies, CSRF tokens and fingerprints are never logged. Each committed write emits one structured line: `action=<message_sent|message_edited|message_deleted> room_id=… message_id=… actor_id=… request_id=…` (no content).

### Where Part 4 hooks in (done: see [Part 4](#part-4-durable-room-events-and-reliable-publication-of-live-updates))
Add the room-event and outbox inserts, and the per-room event-sequence increment, **inside the same transaction** in the three service functions, immediately before their single commit point: `send_message`, `edit_message`, `delete_message` in `app/services/messages.py`. The room lock they already hold is the lock the sequence counter needs. Room events delivered later go to **all active members regardless of role** (chat is open to viewers too). Emit an event only at the points where `created` (send) or `changed` (edit, delete) is set to true: those are exactly the writes that changed something. Matching retries, no-op edits and repeated deletes also reach the end of those blocks and must **not** emit events. Routes never write these tables (a test enforces it).

### Manual demonstration (three real Google accounts)
**Status: pending**: no Google credentials were available. `tests/test_manual_part3_sequence.py` runs the same script against fixture users (sessions created by Part 1's `create_session()`), which proves the script and the flow but **not** the real Google login.

1. Start the backend with a small limit, e.g. `MESSAGE_WRITES_PER_MINUTE=5`.
2. Sign in as **A** (owner), **B** (editor) and **C** (viewer; viewers can chat) through the real Google flow in three separate browser profiles.
3. Copy each `tf_session` cookie *value* into `TF_COOKIE_A/B/C` without putting it on a command line or in a transcript (PowerShell: `$env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput`).
4. From `backend/`: `uv run python -m scripts.manual_part3_check` (add `$env:TF_INSPECT_DB = "1"` to print message and bucket rows: lengths and a tombstone flag, never text or fingerprints).

The script: A creates a room and invitations, B joins as editor, C as viewer → B sends → A fetches history → B retries (same id returned, 200) → B reuses the id with different text (409) → B edits with the current version, then an old one (409) → A deletes B's message → history shows a tombstone and a retry of the original send does not restore the text → C (the viewer) **sends a message successfully**, then tries to edit and delete B's message and gets 403 `message_not_author` → A removes B and B can no longer read or write (404) → exceeding the limit returns 429 with `Retry-After`. It prints PASS/FAIL per step and never prints cookies, tokens or CSRF values. Other clients see changes only by fetching history.

### Part 3 limitations, handbook deviations and unverified assumptions
- **Handbook deviations (deliberate):** 5,000 characters, not 8,000; no `message_versions` table, so edited text is overwritten; `expected_version` with **409**, not `If-Match` with 412; the owner cannot edit another person's message; **viewers can send messages, edit their own, and delete their own** (handbook section 5.2 says viewers cannot send chat; here viewer restrictions apply only to documents and sources in later parts, and chat depends only on being an active member).
- **No rate limit on reads.** A failed request from an *active member* that gets past validation (version conflict, not-author, message-not-found, `message_id_reused`, `rate_limited`) still takes the room lock briefly; requests that fail earlier (auth, CSRF, pre-check, validation, 413) never do. Proxy-level request limiting is advisable.
- **No per-room message retention cap** (rooms grow without bound until a later part decides).
- **Fixed-window burst** of up to twice the limit at a minute boundary.
- **No edit idempotency key:** a retried edit whose first attempt landed gets a version conflict and the client must reload.
- **Rotating `MESSAGE_FINGERPRINT_SECRET`** breaks in-flight retries (`message_id_reused`).
- **Part 2 routes** still parse their JSON before authentication (see Part 2 limitations); message routes do not.
- **Unverified:** the real Google login (see above); behaviour behind a real reverse proxy or across several API processes (the tests use one process against one local PostgreSQL); `clock_timestamp()` monotonicity relies on the database host's clock (the Docker VM used for development has been observed stepping backwards by hundreds of milliseconds between connections, which is why no CHECK relates timestamps from different transactions).

---

## Part 4: durable room events and reliable publication of live updates

**What this part adds, in one paragraph.** Every supported change (room created or renamed, member joined, role changed, member removed, message created, edited or deleted) now also writes a durable **room event** and an **outbox** row *in the same PostgreSQL transaction*. A separate **dispatcher** process publishes outbox rows to Redis Pub/Sub; each API process subscribes to Redis, re-reads the event from PostgreSQL, re-checks the viewer's access, and pushes it to that room's open **WebSocket**s. Message writes stay on HTTP. The WebSocket only *delivers* events.

```
HTTP write ──► ONE transaction: change + rooms.event_seq++ + room_events row + outbox row (pending)
                                   │
                  (commit)         ▼
   dispatcher (own process): claim pending rows (lease) ─► PUBLISH to Redis ─► mark processed
                                   │
   Redis Pub/Sub (hint only: {event_id, room_id, room_seq}; keeps nothing)
                                   │
   each API process: one subscription connection ─► hub.on_room_hint(room, seq)   (Part 5: a HINT only)
        wakes the room's connections; each one READS room_events from PostgreSQL in ascending room_seq,
        after a batched access check ─► its bounded outgoing queue ─► writer task ─► browser
```

### Guarantees: what is promised and what is not
Terms: **at-least-once** means "retried until it works, so it may happen twice"; a **lease** is a time-limited claim a worker holds on a row.

| Statement | Status |
|---|---|
| No committed supported mutation lacks its event and outbox row | **Guaranteed** by one transaction; a test audits every service function that records events. |
| A dispatcher crash does not erase pending work | **Guaranteed**: pending rows live in PostgreSQL; an expired lease makes a row claimable again. |
| Publication to Redis may happen **more than once** | **Yes** (at-least-once). Clients de-duplicate by `event_id`. |
| A Redis notification can be **lost** | **Yes**: nobody subscribed, queue overflow, a Redis connection loss, a slow-consumer disconnect. Redis Pub/Sub stores nothing. |
| `outbox.state = processed` means a browser got it | **No.** It means Redis accepted the publish. A frame sent to a socket means the server queued it, not that a browser rendered it. |
| `room_seq` defines the durable order of a room's events | **Yes**: contiguous 1, 2, 3, … per room, allocated under the room lock. Live frames can arrive out of order or twice; order by `room_seq`. |
| A client that missed events can recover them | **From the database**, via `GET /rooms/{id}/events`. Automatic recovery is Part 5. |

This README never claims exactly-once or guaranteed delivery, and neither should clients.

### Running it (from the repository root unless noted)
```bash
docker compose up -d --wait postgres redis        # Redis is bound to 127.0.0.1:6390 only
cd backend
uv run alembic upgrade head                       # applies 0004 (room_events, outbox, rooms.event_seq)
# API processes: run as many as you like on different ports; each holds its own sockets
uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log --no-proxy-headers --ws-max-size 1024 --ws-ping-interval 20 --ws-ping-timeout 20
uv run uvicorn app.main:create_app --factory --port 8001 --no-access-log --no-proxy-headers --ws-max-size 1024 --ws-ping-interval 20 --ws-ping-timeout 20
# One dispatcher is enough (several are safe: rows are claimed with SKIP LOCKED)
uv run python -m app.workers.outbox_dispatcher    # SIGTERM / Ctrl+C stops it gracefully
```
- `--ws-max-size 1024` makes the server itself refuse oversized client frames (close 1009); clients are expected to send nothing. `--ws-ping-interval/--ws-ping-timeout` make uvicorn drop connections whose peer vanished.
- **Run the dispatcher.** Without it everything still works over HTTP and events accumulate as pending outbox rows, but no socket ever receives anything.
- Production needs `REDIS_URL=rediss://…` **with a password** (startup fails otherwise). Redis credentials never reach a browser: sockets talk only to the API.

```bash
uv run python -m app.commands.outbox_status                          # counts, ages, retries, failed entries
uv run python -m app.commands.outbox_requeue --event-id <uuid>        # put ONE failed entry back to pending
uv run python -m app.commands.cleanup_auth --outbox-retention-days 7  # also deletes old PROCESSED outbox rows
```
`outbox_status` never selects payloads. **Suggested alert (documentation only):** the oldest pending row is older than 2 minutes. The dispatcher and each API process log a stats line every 30 s (counts and codes only; no payloads, message text, cookies or tokens).

### Configuration added in Part 4
| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6390/0` | Redis/Valkey. Production: `rediss://` + password. |
| `REDIS_CHANNEL_PREFIX` | `threadforge` | Channel is `<prefix>:room:<room_id>`. Letters, digits, `-`, `_`, `.`. |
| `REDIS_SOCKET_TIMEOUT_SECONDS`, `REDIS_HEALTH_CHECK_INTERVAL_SECONDS` | 5, 30 | Client timeouts. |
| `OUTBOX_BATCH_SIZE` / `OUTBOX_POLL_INTERVAL_MS` | 50 / 200 | Rows per claim / idle poll. |
| `OUTBOX_CLAIM_LEASE_SECONDS` | 30 | How long a claim lasts. **Must be ≥ 2 × `OUTBOX_PUBLISH_TIMEOUT_SECONDS`** (startup fails otherwise). |
| `OUTBOX_PUBLISH_TIMEOUT_SECONDS` | 2 | Per-publish bound. |
| `OUTBOX_RETRY_MAX_DELAY_SECONDS` | 60 | Backoff cap (base 0.5 s, doubling, equal jitter: the delay is uniform in [cap/2, cap]). |
| `OUTBOX_PROCESSED_RETENTION_DAYS` | 7 | Used by the cleanup command. |
| `WS_MAX_CONNECTIONS_PER_USER` / `WS_MAX_CONNECTIONS_PER_PROCESS` | 5 / 1000 | Per API process; over the cap → close 4409. |
| `WS_SEND_TIMEOUT_SECONDS`, `WS_CONNECTION_QUEUE_SIZE` | 5, 256 | A frame send is bounded; a send timeout → close 4429. |
| `WS_SUBSCRIBE_TIMEOUT_SECONDS` | 5 | Redis SUBSCRIBE acknowledgement bound (then degraded mode, Part 5). |

### Schema (migration `0004`)
- `rooms.event_seq BIGINT NOT NULL DEFAULT 0 CHECK (event_seq >= 0)`: the room's last allocated sequence number. Incrementing it changes neither `version` nor `updated_at`.
- `room_events`: primary key `(room_id, seq)`; `event_id` unique; unique `(room_id, seq, event_id)`; `type` CHECK of the eight types; `payload` a JSON object ≤ 2048 bytes; `created_at` from `clock_timestamp()`.
- `outbox`: primary key `event_id`; **one composite foreign key** `(room_id, room_seq, event_id)` → `room_events(room_id, seq, event_id)`, so an outbox row can never name an event that disagrees with itself; a CHECK matrix ties `state` (`pending`/`processing`/`processed`/`failed`) to the claim and error columns; partial indexes serve the dispatcher's queries.
- **No consumer-receipts table.** Redis Pub/Sub cannot tell the publisher who received what, so a table of receipts would invent evidence. Delivery is a hint; recovery is from `room_events`.
- **Sequence allocation is `UPDATE rooms SET event_seq = event_seq + 1 … RETURNING` under the room lock** (not a PostgreSQL SEQUENCE, which leaves gaps on rollback, and not `MAX(seq)+1`, which races). `record_room_event(locked_room, …)` requires a `LockedRoom` handle that only `lock_room()` / `adopt_new_room()` can produce, so an event cannot be recorded without holding the lock.

### Event types and payloads (`app/schemas/events.py`)
| `event_type` | `resource_type` | `resource_version` | `payload` |
|---|---|---|---|
| `room_created`, `room_renamed` | `room` | null | `{}` |
| `member_joined`, `member_role_changed` | `membership` | null | `{"user_id", "role"}` |
| `member_removed` | `membership` | null | `{"user_id"}` |
| `message_created`, `message_edited`, `message_deleted` | `message` | the version reached | `{}` |

Events are change notifications, not copies of data: they never contain message text (live or deleted), invitation tokens, emails, credentials or request bodies. Creating or revoking an invitation emits nothing; neither does any no-op (a repeated send, a rename to the same name, a repeated delete).

### WebSocket: `/rooms/{room_id}/ws`
**Handshake (all before the connection is accepted).** The server checks (1) `Origin` exactly matches a `TRUSTED_ORIGINS` entry (a **missing** Origin is rejected: this is a browser endpoint), (2) the session cookie, via the same function the HTTP routes use, (3) the user is an **active member**. Every failure, including an unknown room, is the same bare **HTTP 403**. Credentials are never accepted in the URL. The Origin check is not CORS (browsers do not apply CORS to WebSockets, and cookies ride along on cross-site handshakes), so without it any web page could open an authenticated socket as the visitor.

**Frames, close codes and delivery were redefined in Part 5** (the Part 4 `ready` frame, the 1008 rule, the 1013-on-no-subscription rule and the notification queue + forwarding workers are gone). See [Part 5](#part-5-reconnect-recovery-presence-typing-indicators-and-live-access-enforcement) for the current protocol, the single close-code table and the delivery rules. What stays from Part 4: the handshake above, the caps, the guarantees table, the schema and the dispatcher.

### History: `GET /rooms/{room_id}/events?after_seq=0&limit=100`
Active members only (404 `room_not_found` for everyone else, like the other room routes). Returns `{"items":[…], "next_after_seq", "has_more", "current_room_seq"}` in ascending `room_seq`, from **one SQL statement** so `items` and `current_room_seq` agree. `limit` is 1–500 (default 100); values outside the range are **422, never clamped**. `after_seq` beyond the room's latest event is **409 `cursor_ahead`** with `current_room_seq` (it also happens after a database restore). `Cache-Control: no-store`. Works with Redis down. Part 4 does not prune events.

### The dispatcher (`python -m app.workers.outbox_dispatcher`)
1. **Claim** (one statement): select due pending rows `FOR UPDATE SKIP LOCKED`, set `state='processing'`, a fresh `claim_token`, `claimed_until = now() + lease` and `attempts + 1`, using the **database clock**. Expired `processing` rows are claimable again, which is how a crashed dispatcher's work comes back.
2. **Publish** each row with a bounded timeout. The envelope `{schema_version, event_id, room_id, room_seq}` (no event body) must be valid **and agree with the row**, otherwise the row fails permanently (`envelope_invalid`).
3. **Finish**, always matching on `claim_token`: processed, retry with backoff, failed, or released. A worker whose lease expired and was reclaimed updates zero rows and moves on. **Deadline rule:** once the lease is nearly over, a worker stops publishing the rest of its batch and releases it instead of publishing under a lease someone else may now hold.
4. **Graceful stop** on SIGTERM/SIGINT (Ctrl+Break on Windows): finish the in-flight row, release unstarted claims, exit.

Per-room ordering of *publication* is not promised (retries, several dispatchers); the sequence number is the order.

### Redis outage and other failures (as of Part 5)
| Situation | Behaviour |
|---|---|
| Redis unreachable | HTTP reads and writes keep working and keep committing events and **pending** outbox rows; `/health/ready` stays DB-only (healthy); the dispatcher backs off and retries; WebSocket connections open and stay open in **degraded mode**: durable events still arrive through reconciliation (≤ 2 s) and presence/typing say `degraded: true` |
| Redis connection lost while sockets are open | Sockets stay open; each API process reconnects with backoff, resubscribes and wakes every connection; hints published during the gap are lost, which reconciliation covers |
| Dispatcher stopped | Writes succeed; events pile up as pending; **sockets still receive them** through reconciliation (≤ one interval); on restart the dispatcher publishes the pending rows (late hints repeat nothing) |
| Dispatcher crashes mid-batch | Leases expire (30 s default) and another dispatcher republishes: possible duplicate hints, which are harmless |
| Slow browser | Only that socket is closed (4429) |
| Malformed, oversized or wrong-channel Redis message | Dropped and counted; nothing about the payload is logged |

### Manual demonstration (two real Google accounts, two API processes)
**Status: real Google login pending** (no credentials were available). The script was run end to end against **real processes**: two uvicorn API processes (8000, 8001), a dispatcher process, the development PostgreSQL and this project's compose Redis, with two users whose sessions were created by Part 1's `create_session()` (as in the test fixtures; no endpoint exists for this) and an operator that really stopped/started the dispatcher process and the Redis container. All steps passed. Observed in those process logs: the dispatcher retried with backoff (`code=redis_connection`, then `redis_timeout`) while Redis was down and published the outage-time event on attempt 3 after Redis returned; both API processes logged `redis_pubsub_lost … notification_gap_possible=true`, reconnected and resubscribed; no message text, cookie or token appeared in any log. `tests/test_manual_part4_sequence.py` runs the same script against two in-process API servers (real uvicorn, real WebSockets, real Redis) with the Redis step left to the human (reported SKIP there); `tests/test_realtime_failures.py` covers Redis outage behaviour separately. The demonstration left two `Demo A/B` users and a room in the development database.

1. Start Postgres, Redis, two API processes (ports 8000 and 8001) and the dispatcher as above.
2. Sign in as **A** (owner) and **B** through the real Google flow in two browser profiles and copy each `tf_session` cookie *value* into `TF_COOKIE_A/B` without putting it on a command line (PowerShell: `$env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput`).
3. From `backend/`: `uv run python -m scripts.manual_part4_check` (add `$env:TF_OPERATOR = "1"` to be prompted to stop/start the dispatcher and Redis).

The script: A creates a room, B joins → A on API 1 and B on API 2 open sockets, resume from the room's seq and reach `sync_complete` → A sends one message and **both** sockets receive the same `message_created` (no text in the frame) → it is in durable history → *(operator)* with the dispatcher stopped the write succeeds and (since Part 5) both sockets still receive it through reconciliation, and the late hint repeats nothing → *(operator)* with Redis stopped writes succeed and `/health/ready` is 200 → an unknown client frame closes **4400** → bad origin / no origin / no cookie are all 403 → A removes B: B's socket closes **4403 with no frame**, A receives `member_removed`, B can no longer read history (404) or reconnect (403). It never prints cookies, tokens or message text.

### Where Part 5 hooks in (done: see [Part 5](#part-5-reconnect-recovery-presence-typing-indicators-and-live-access-enforcement))
Replay-to-live recovery, the resume cursor, proactive access enforcement and presence are implemented. Still open: event retention (a cursor that is too *old* is only `too_far_behind` by count) and a new event type needs one entry in `EVENT_SPECS`, the migration's type CHECK, and the service function that calls `record_room_event`.

### Part 4 limitations, handbook deviations and unverified assumptions
- **Not implemented in Part 4 (implemented in Part 5):** reconnection, cursors, replay-to-live recovery, presence, typing, proactive revocation. Still out of scope: AI, uploads, document collaboration.
- **Idle connections** (resolved in Part 5): a periodic sweep and the hint-triggered check now close expired or removed connections, idle ones included.
- **Lost notifications** (resolved in Part 5): hints are only wake-ups; reconciliation and replay recover everything from PostgreSQL. Duplicates and reordering of *hints* are harmless.
- **No event pruning**; `room_events` grows without bound, and processed outbox rows are removed only by the cleanup command.
- **Connection caps are per process**, not global across API processes.
- **Handbook deviations (deliberate):** no event retention or compaction yet; the event payload is inline (ids and roles only) rather than a pointer plus snapshot; no consumer-receipts table; the WebSocket path `/rooms/{room_id}/ws` and the close codes above are this project's own.
- **Verification at completion:** the full suite (`uv run pytest`, real PostgreSQL 16 and Redis 7 in Docker) passed with **827 tests**; `ruff check`, `ruff format --check` and `alembic check` are clean.
- **Pre-existing test flake observed, not caused by Part 4:** `tests/test_messages_history.py::test_complete_history_is_visible_regardless_of_when_a_member_joined` failed once in a full run and passed on three isolated reruns. Message history orders by `created_at` (`clock_timestamp()`), and the Docker VM's clock has been observed stepping backwards between connections (see Part 3 limitations).
- **Unverified assumptions:**
  - Real Google login and a real browser: tests use the `websockets` client library with explicit `Cookie`/`Origin` headers, never a browser. A browser cannot read the HTTP status of a rejected handshake (it only sees a failed connection).
  - A reverse proxy in front of the API (WebSocket upgrade, `Origin` pass-through, idle timeouts versus the ping interval) was not exercised.
  - Redis over TLS (`rediss://`) with a password was validated in configuration only; tests run against a local Redis 7 without TLS. Valkey and managed Redis services were not tested; their `client-output-buffer-limit pubsub` settings can differ (Redis closes slow Pub/Sub subscribers; the manager reconnects).
  - Development and tests ran on Windows with Docker Desktop; the SIGTERM paths for uvicorn and the dispatcher on Linux were not run here (Windows uses Ctrl+Break).
  - Starlette's "close before accept is an HTTP 403 handshake rejection" was read from source and then confirmed empirically by the handshake tests.
  - Several dispatchers or API processes against one PostgreSQL were tested on one machine only.

---

## Part 5: reconnect recovery, presence, typing indicators and live access enforcement

**In one paragraph.** A client now starts from a consistent **bootstrap** (`GET /rooms/{id}/sync`: state and a `through_seq` from one database snapshot), opens the WebSocket and sends `resume{after_seq: through_seq}`. The server replays every event after that cursor **read from PostgreSQL in ascending `room_seq`**, says `sync_complete`, and then keeps the connection live the same way: a Redis message is only a **hint** ("something newer exists") that wakes the connection, which then reads PostgreSQL again. A lost hint is found by **reconciliation** within one interval. Acknowledgements give flow control. A reconnecting client resumes from its own cursor. Presence ("who is online", availability) and typing live only in Redis. Access (session valid, user active, member active) is enforced for the whole life of a connection, including idle ones.

Terms. **Cursor**: the highest `room_seq` a client has fully processed. **Hint**: a tiny Redis message `{room, seq}` that only wakes connections. **Reconciliation**: a periodic query that compares each room's `event_seq` with what its connections have been sent. **TTL**: an expiry after which Redis deletes a key by itself. **Snapshot**: a PostgreSQL transaction that sees one fixed committed state.

### What Part 5 replaced from Part 4
| Part 4 | Part 5 |
|---|---|
| Notification queue + forwarding workers pushed an event for every notification (lost hint = lost event; parallel workers could reorder) | `app/realtime/room_hub.py`: a hint only wakes connections. Each connection **pulls** durable events in order (`app/realtime/connection.py`). No path pushes an event that was not read from PostgreSQL in sequence |
| `ready{current_room_seq}` | `sync_started` … events … `sync_complete`. **Only `sync_complete` means synchronized**; an open socket does not |
| Any client frame closed the socket with **1008** | Client frames exist (`resume`, `ack`, `heartbeat`, `typing`, `set_availability`); **1008 is retired**, a bad frame is **4400** |
| No subscription closed the socket **1013** | Redis trouble means **degraded mode**: the connection stays open, durable delivery continues through reconciliation, presence/typing say `degraded: true` |
| Access noticed only when the next event was forwarded | Hint-triggered check, periodic sweep of every connection (idle ones too), check before every page |
| `REALTIME_QUEUE_SIZE`, `WS_FORWARD_WORKERS`, `ws_connection_queue_size = 100` | Removed / default 256 |

Part 4 tests that changed: `test_websocket.py`, `test_redis_subscriptions.py`, `test_realtime_failures.py`, `test_manual_part4_sequence.py` (and `scripts/manual_part4_check.py`; it now passes with *no dispatcher at all*, which Part 4 expected to fail). **No migration** was needed (`alembic check` is clean): everything durable already existed, and presence/typing never touch PostgreSQL.

### Guarantees now
| Statement | Status |
|---|---|
| A connection receives every retained event after its cursor **in ascending `room_seq`, none skipped** | Yes: events come only from `room_events` by primary-key range; contiguity is verified before anything is sent |
| A gap while draining is a bug, never a race | Yes. Sequence numbers are allocated under the room lock, held until commit, so **committed events form a gapless prefix**. A gap sends `resync_required(event_gap)`, closes **4410** and logs an error with the room and sequences. It never skips |
| A lost, duplicated or reordered Redis hint loses an event | **No.** It can only delay a wake-up by up to one reconcile interval |
| Duplicates | Possible at the client (reconnects); ignored by `room_seq` |
| Acknowledgements are read receipts or have durable effect | **No.** They drive flow control and diagnostics only; the cursor lives in the client |
| Exactly-once / guaranteed delivery | **Never claimed.** A frame means the server queued it, not that a browser rendered it |
| Presence/typing in PostgreSQL, `room_events` or the outbox | **Never** (tested: table counts and `event_seq` are unchanged) |

### `GET /rooms/{room_id}/sync`: the bootstrap
Active member only (404 otherwise), `Cache-Control: no-store`. Returns `room` (id, name, owner_id, created_at, updated_at), the caller's `role`, `membership_epoch`, `members` (all *active* members: user_id, display_name, role, joined_at; no emails; bounded by the member cap), `messages` (the latest 50, ascending, tombstones included, Part 3 representation), `next_before` (the Part 3 history cursor) and `through_seq` (`rooms.event_seq` in the same snapshot; 0 for a room with no events).

**Why it is consistent.** Every mutation changes the data, bumps `rooms.event_seq` and inserts its `room_events` row in **one transaction while holding the room lock**. A `READ ONLY REPEATABLE READ` transaction sees every statement against one committed state, fixed by its **first query**, so it sees each such transaction entirely or not at all: `through_seq` counts exactly the changes the body reflects (`app/services/sync.py`). PostgreSQL does not allow changing the isolation level after a transaction's first query (verified in its docs and by a test), and authentication has already run queries on the request session, so the snapshot uses **its own session** with the isolation level set before its first statement. The first statement re-checks membership and reads the room in the same snapshot; no Redis or socket work happens inside. A test commits a message and a new member *between the snapshot's reads* and shows neither appears, and that the same interleaving on a plain `READ COMMITTED` session *does* mix.

Older message pages loaded later (`GET …/messages?before=`) are ordinary current-state reads; the client merges them by id and version.

### WebSocket protocol (schema_version 1): `/rooms/{room_id}/ws`
Unchanged from Part 4: cookie auth through the shared session function, active-membership check, **exact Origin match** (missing Origin rejected), the uniform pre-accept **HTTP 403**, per-user (5) and per-process (1000) caps (close 4409). Client frames are JSON text of at most **1 KiB**, unknown fields rejected, with **no room or user identity inside** (identity always comes from the authenticated connection).

**Client → server** (`app/realtime/protocol.py` is authoritative):
```json
{"type":"resume","schema_version":1,"after_seq":41}      // MUST be first, within RESUME_TIMEOUT (5 s); integer >= 0
{"type":"ack","through_seq":60}
{"type":"heartbeat"}
{"type":"typing","state":"start"}                        // or "stop"
{"type":"set_availability","value":"busy"}               // available | busy | dnd
```
**Server → client:**
```json
{"type":"sync_started","schema_version":1,"room_id":"…","after_seq":41,"through_seq":57}
{"type":"room_event", …}                                 // the Part 4 shape, unchanged
{"type":"sync_complete","through_seq":57}
{"type":"heartbeat_ack","server_time":"…"}
{"type":"presence_snapshot","room_id":"…","online":[{"user_id":"…","availability":"available"}],"degraded":false}
{"type":"typing_state","room_id":"…","typing":["<user_id>"],"degraded":false}
{"type":"resync_required","reason":"cursor_ahead" | "too_far_behind" | "event_gap"}   // then close 4410
```
**Handshake sequence** (after the pre-accept checks and accept): the caps (4409); wait for `resume` (else **4408**; invalid **4400**); acquire a **replay slot** from a process-wide semaphore (`REPLAY_CONCURRENCY` 20; waiting longer than `REPLAY_SLOT_WAIT_SECONDS` closes **1013**); register with the room hub; ensure the Redis room subscription and wait for its acknowledgement with a bounded timeout, **continuing in degraded mode** if that fails; read `H = rooms.event_seq` *after* the subscription step; `after_seq > H` → `cursor_ahead`, `H - after_seq > REPLAY_MAX_EVENTS` (1000) → `too_far_behind` (both then 4410); send `sync_started`; drain `(after_seq, H]` in pages of `REPLAY_PAGE_SIZE` (100); send `sync_complete(H)`; release the slot; mark the connection live; send the first `presence_snapshot` and `typing_state`; run one more drain pass (events committed after H) and then wait for wake-ups.

**Close codes** (one table for Parts 4 and 5):
| Code | Meaning | Client reaction |
|---|---|---|
| 1001 | Server shutting down (uvicorn itself sends 1012 when the whole process stops) | Reconnect with backoff |
| 1003 | Binary frame | Terminal protocol bug |
| 1009 | Frame too large | Terminal protocol bug |
| 1013 | Try again later (overload, replay slot wait, database unavailable too long) | Reconnect with backoff |
| 4400 | Protocol violation (bad/unknown frame, invalid ack, unsupported version) | Terminal |
| 4401 | Session invalid (revoked, expired, user inactive) | Terminal; sign in again |
| 4403 | Not a member (access revoked); **no frame is sent first** | Terminal; clear room data |
| 4408 | Resume or heartbeat timeout | Reconnect with backoff |
| 4409 | Too many connections | Retry after at least 10 s, stop after 3 tries |
| 4410 | Resync required | Fetch bootstrap, reconnect |
| 4429 | Slow consumer or frame-rate exceeded | Reconnect with backoff; 3 times in 5 minutes means resync |

### Server design
**Room hub and hints** (`room_hub.py`, `subscriptions.py`). The Redis reader validates every notification (≤ 1 KiB, JSON, schema, room matches channel) and then does only `hub.on_room_hint(room, seq)`: `hint_seq = max(hint_seq, seq)` and `Event.set()` on the room's connections. Memory per room is constant (a test sends 10,000 hints and checks the state is one integer), so a flood cannot pile up or make Redis disconnect a slow subscriber. Hints for rooms with no local connection are ignored. A hint also bumps the room's `access_epoch`, which invalidates cached access verdicts (below).

**Per-connection tasks** (`connection.py`): *receiver* reads and validates client frames and rate-limits them (20 frames/s sustained, 10 acks/s; typing frames above 2/s are dropped and counted; the sustained budget closes 4429); it never waits on anything but the socket, so acks and heartbeats keep flowing while the drainer is paused. *Drainer* is the only producer of durable frames: pull-based, it holds only `last_sent` and `last_acked`. *Writer* is the only task that writes to the socket (including the close frame); every send has a timeout (`WS_SEND_TIMEOUT_SECONDS` 5); the outgoing queue is bounded (256) and presence/typing frames are **coalesced** to one pending frame each. *Watchdog* closes **4408** when no client frame of any kind arrived within `WS_WATCHDOG_TIMEOUT_SECONDS` (45). If one task ends, the others are cancelled and `finally` unregisters from the hub, releases the Redis subscription reference, queues presence/typing removal, and closes the socket.

**Drain loop.** Each pass: `ensure_access` (below) → read **one page** `WHERE room_id = :r AND seq > :last ORDER BY seq LIMIT :n` (capped to the remaining ack window) → **close the database session** → verify contiguity (first row is `last+1`, each next is previous+1; otherwise `event_gap`) → queue the page → repeat until caught up → wait for a wake-up. No database session is ever held while waiting on a socket. A page read before a revocation and queued after it is an unavoidable race (see limits). A hint that is ahead of the database (bogus or early) is discarded after one empty read.

**Flow control and acks.** `unacked = last_sent - last_acked`. At `ACK_WINDOW` (200) draining pauses until an ack arrives (pages are capped so the window is never exceeded). If acks make no progress for `ACK_TIMEOUT` (30 s) the client is closed **4429**. Replay obeys the same rule, and the watchdog keeps running during a pause, so a slow replay never looks like a dead connection. An ack is valid if it is an integer, at least the accepted resume point and at least `last_acked` (equal is ignored) and at most `last_sent`; anything else closes **4400**.

**Reconciliation** (`RealtimeHub.reconcile_once`). Every `RECONCILE_INTERVAL_SECONDS` (5, jittered by up to 20 %) **one query for all rooms that have local connections** (`SELECT id, event_seq FROM rooms WHERE id = ANY(:ids)`) updates each room's known sequence and wakes connections whose `last_sent` is behind. Not one query per connection. Every connection is also woken after a Redis subscription recovers. While Redis is unhealthy the interval is `RECONCILE_INTERVAL_DEGRADED_SECONDS` (2). **Worst-case delay for a lost hint: one interval** (plus jitter).

**Redis down** (verified on real processes, with the actual container stopped): HTTP writes keep working; existing and new connections keep receiving durable events through reconciliation; `presence_snapshot`/`typing_state` carry `degraded: true` with empty lists (unknown, **not** "everyone is offline"); nothing is lost. When Redis returns, every process re-subscribes, wakes all connections and re-registers its local connections, which rebuilds presence.

### Live access enforcement
Checked: the session exists, is not revoked, `expires_at > the database clock`, the user is `active`, and the membership is `active`. **No role checks** (chat is open to every active member).
* at the handshake; before replay starts; **before every replay page and every live batch**, through the room hub's **batched check** (ONE query per room for all its local connections, listing their session ids), the result reused for at most `ACCESS_CACHE_SECONDS` (1) **unless a hint arrived since** (the room's `access_epoch` changed);
* before applying `typing` or `set_availability`;
* **on every hint for the room**, so a member removal (which commits an event) closes the removed member's connection within a moment, *without sending them anything*, because their drain re-checks before it reads;
* **periodically** (`ACCESS_CHECK_INTERVAL_SECONDS`, 15): one batched query per room per process, for **every connection including idle ones**.

On access loss: the connection's pending frames are **discarded**, it closes **4401** (session) or **4403** (membership), is unregistered, its presence/typing entries are removed (best effort) and its subscription reference is released. A removed member never receives their own `member_removed` frame. Logout, session expiry and a disabled account emit **no room event**, so they are bounded by the periodic sweep (at most 15 s + jitter by default). A lost removal hint is bounded the same way.

**PostgreSQL unavailable.** Nothing protected is sent on stale assumptions: every read retries with backoff for up to `ACCESS_DB_GRACE_SECONDS` (30); after that **every connection closes 1013**, idle ones included. Each database call is bounded by its own timeout (`grace / 3`, 0.3–10 s), so a *hung* database counts as a failure too (tested).

**Unavoidable limits.** Frames already written or in flight cannot be recalled, so the exposure window after a revoke is the page being sent, or for an idle connection up to the check interval when no hint arrives. This is documented, not hidden.

### Presence and typing (Redis only)
`app/realtime/presence.py` (Lua) and `presence_hub.py` (workers, ticks). **Every key has a TTL.**
* `{prefix}:presence:{room}:conns`: sorted set, member `"{user_id}:{conn_id}"`, score = expiry in ms. `conn_id` is a server-generated random value, never sent to clients. No IPs, no user agents.
* `{prefix}:presence:{room}:avail`: hash `user_id → available|busy|dnd`, shared by that user's tabs (last write wins). **Availability is per room and per user (not global)**, display-only (it never changes permissions or delivery), meaningful only while the user has a live connection, and resets to `available` when they come back from offline.
* `{prefix}:typing:{room}`: sorted set, member `"{user_id}:{conn_id}"`, score = expiry; aggregated by distinct user, so one tab's `stop` never clears another tab's typing.

Each operation is **one Lua script** (atomic) that reads **Redis `TIME`** inside the script, so API servers' clocks do not matter (allowed with effects replication, the default since Redis 5; all keys are passed in `KEYS`). A connection is registered on connect, refreshed by its heartbeat (and by the server's tick for connections not refreshed for a while), removed on disconnect, and expires `PRESENCE_EXPIRY_SECONDS` (45) after a crash. `presence_snapshot` is always the full state (a room has at most 50 members). A per-process, per-room **tick** every `PRESENCE_TICK_SECONDS` (15, jittered) rebuilds it and sends it **only if it changed**; it is also how expiries (which Redis never announces) are reported. Connect, disconnect, availability and typing changes publish a hint on `{prefix}:presence-hint:{room}`; other processes re-tick on it, coalesced to **at most one presence tick per second and two typing ticks per second per room**. A lost presence hint costs at most one tick. Snapshots are **filtered to current active members** (a cached member set, refreshed when a member event is drained and at least every 15 s); if membership cannot be determined nothing is sent. Typing: any active member may type (viewers too); the server derives user and room from the connection; at most 2 typing frames per second per connection (extras dropped and counted); no keystrokes or draft text are stored.

### The client (`client/`)
A framework-free TypeScript module with an injectable socket, `fetch`, timers and random (so it is unit-tested with a fake socket and a fake clock). `npm install && npm test` (61 vitest tests), `npm run build`. `src/room-client.ts` is the state machine; `demo/cli.mjs` (Node + `ws`) and `demo/index.html` are minimal demos; `test/e2e-driver.mjs` drives the real client for the backend's end-to-end tests.

* **State** (room, members, messages by id, **cursor**, status `bootstrapping | syncing | live | reconnecting | resyncing | terminal`) lives in memory, with the cursor, installed together from a bootstrap. A page refresh bootstraps; the cursor is never persisted alone.
* **Events**: frames arrive in order; `seq <= cursor` is a duplicate (ignored); a gap (`seq != cursor+1`) closes the socket and reconnects from the last processed cursor. Batches (until `sync_complete`, 100 events, or 50 ms idle) compute the distinct affected message ids, whether the room is dirty (`room_renamed`) and whether members are dirty. More than 20 distinct messages → a fresh bootstrap; otherwise fetch each message by id (concurrency 4), the members (all pages) and the room if dirty. A fetched message replaces the local one only if its version is `>=` the local version; **a tombstone is final**. The cursor advances and the ack is sent **only after every required fetch succeeded**; a transient failure retries 3 times with backoff, then reconnects from the last processed cursor; 401/403/404 follows access loss. `member_removed` for the current user stops everything, clears the room and never reconnects. Batches are processed *during replay* too (otherwise the server's ack window would deadlock a long replay).
* **Reconnect**: exponential backoff with jitter (1 s doubling to 30 s, reset after 30 s live), **one** reconnect loop, a generation counter so frames and timers of an obsolete socket are ignored; switching rooms cancels the old socket, timers and fetches (AbortController). A refused handshake looks like an abnormal close (1006) with no frames in a browser, so the client then **re-bootstraps over HTTP**, where 401 means sign in again and 404 means access lost. More than 3 resyncs in 60 s is a terminal "cannot synchronize" state.
* **Heartbeat** every 15 s; no server frame of any kind for 45 s → reconnect. **Typing**: `start` at most every 3 s, `stop` after 4 s idle or on send; typing entries also expire locally after 6 s; `degraded: true` is shown as "unknown", never "offline". **Sends** are retried with the same `client_message_id` (safe, Part 3); a reconnect never generates new ids or re-sends confirmed writes.

**Why an application heartbeat in addition to transport ping/pong.** uvicorn's ping/pong (`--ws-ping-interval/--ws-ping-timeout`) is kept: it frees server resources for dead peers. But browsers answer pings automatically and JavaScript **cannot observe them**, so the page cannot tell a dead connection from a quiet one; the heartbeat frame and `heartbeat_ack` give it something to watch. Browsers may throttle timers in hidden tabs (not verified against current versions), which can cause a missed heartbeat and a reconnect; the cursor makes that safe.

### Configuration added in Part 5 (all validated at startup; defaults shown)
| Variable | Default | Meaning / validation |
|---|---|---|
| `WS_RESUME_TIMEOUT_SECONDS` | 5 | `resume` must arrive within this, else 4408 |
| `REPLAY_PAGE_SIZE` / `REPLAY_MAX_EVENTS` | 100 / 1000 | page size ≤ max; further behind than max → `too_far_behind` |
| `REPLAY_CONCURRENCY` / `REPLAY_SLOT_WAIT_SECONDS` | 20 / 10 | connections replaying at once per process; longer wait → 1013 |
| `ACK_WINDOW` / `ACK_TIMEOUT_SECONDS` | 200 / 30 | **`ACK_TIMEOUT` must exceed `WS_SEND_TIMEOUT`** |
| `WS_CLIENT_FRAMES_PER_SECOND` / `WS_ACK_FRAMES_PER_SECOND` / `WS_TYPING_FRAMES_PER_SECOND` | 20 / 10 / 2 | rate limits (typing extras are dropped) |
| `WS_CONNECTION_QUEUE_SIZE` | 256 | outgoing frames per connection |
| `RECONCILE_INTERVAL_SECONDS` / `…_DEGRADED_SECONDS` | 5 / 2 | degraded must not exceed normal |
| `ACCESS_CHECK_INTERVAL_SECONDS` / `ACCESS_CACHE_SECONDS` / `ACCESS_DB_GRACE_SECONDS` | 15 / 1 / 30 | cache must be smaller than the interval |
| `CLIENT_HEARTBEAT_INTERVAL_SECONDS` / `WS_WATCHDOG_TIMEOUT_SECONDS` | 15 / 45 | **watchdog ≥ 2 × heartbeat** |
| `PRESENCE_EXPIRY_SECONDS` / `PRESENCE_TICK_SECONDS` | 45 / 15 | **expiry ≥ 2 × heartbeat** |
| `PRESENCE_HINT_MIN_INTERVAL_SECONDS` / `TYPING_EXPIRY_SECONDS` / `TYPING_STATE_MIN_INTERVAL_SECONDS` | 1 / 5 / 0.5 | coalescing and expiry |

### Operations: database load
Per API process, with `R` rooms that have local connections and `C` connections:
* reconciliation: **one query per `RECONCILE_INTERVAL`** regardless of `R` and `C` (12/min at 5 s);
* periodic access sweep: `R` queries per `ACCESS_CHECK_INTERVAL` (4 per room per minute at 15 s);
* presence member refresh: at most one query per room per 15 s (plus one when a member event is drained);
* live delivery: when a room's events arrive, each *woken connection* does one page read and shares one access query per room (re-run per hint burst): roughly `C_room` page reads + 1 access query per event burst. **This is the dominant cost** and scales with connections × event bursts; there is no shared per-room event cache in Part 5;
* replay: `ceil(missed / 100)` page reads per reconnecting connection, limited to `REPLAY_CONCURRENCY` at once;
* heartbeats, typing and availability are Redis only.

Example: 200 connections in 20 rooms, idle: 12 + 80 + 80 ≈ **170 queries/minute**; each message in a 10-member room adds about 10 page reads + 1 access query. Alert suggestions (documentation only): `realtime_stats` log lines every 30 s show `connections`, `replaying`, `redis_healthy`, `resyncs` and `closes` by code; investigate a rising count of 4410 (resync), 4429 or 1013.

### Manual demonstration (three real Google accounts, two API processes)
**Status: real Google login pending** (no credentials were available). `scripts/manual_part5_check.py` plays a client (bootstrap, resume, fetch-and-merge, ack) and judges convergence against a **fresh bootstrap**, never against itself. Environment: `TF_COOKIE_A/B/C` (cookie *values*, set with `Read-Host -MaskInput`, never on a command line), `TF_BASE_URL_1/2`, `TF_COOKIE_NAME`, `TF_ORIGIN`, `TF_OPERATOR=1` to be prompted for the steps that need you to stop or restart something. Steps: bootstrap and live sync across two API processes → a message A→B → two tabs for A are one online user → typing appears and expires → B disconnects, A creates/edits/deletes while B is away, B resumes from its cursor and its state equals a fresh bootstrap with an ordered duplicate-free replay and no resurrected text → *(operator)* dispatcher stopped: the event still arrives through reconciliation → *(operator)* Redis stopped: writes succeed, B still receives the event, presence says `degraded`; Redis restarted: presence is rebuilt → *(operator)* API process 1 restarted: reconnect through the other and converge → C is removed: socket closes 4403 with no frame, A is told, C is refused (403/404) → B logs out: its idle socket closes 4401 within the check interval and the reconnect is refused. Never prints cookies, tokens or message text.

It was run end to end against **real processes** (two uvicorn servers, a dispatcher process, the development PostgreSQL and the compose Redis container, with the operator really stopping the dispatcher, the Redis container and an API process; sessions made by Part 1's `create_session()` as in the fixtures; **default intervals**). All steps passed, and the process logs held no errors, tracebacks, message text or cookies. `tests/test_manual_part5_sequence.py` runs the same script in-process (the Redis and API-restart steps are SKIP there); `tests/test_client_e2e.py` runs the real TypeScript client (Node) against a live backend.

### Where to read the code (suggested order)
`app/services/sync.py` (snapshot) → `app/realtime/protocol.py` (frames, close codes) → `app/realtime/room_hub.py` (hints, reconciliation, access) → `app/realtime/connection.py` (the four tasks, the drain loop) → `app/realtime/subscriptions.py` (hints, degraded mode) → `app/realtime/presence.py` / `presence_hub.py` → `client/src/room-client.ts` → the tests named below.

### Tests
Backend: `cd backend && uv run pytest` (real PostgreSQL 16 and Redis 7; Part 4 test isolation: database `*_test`, Redis index 15, a unique channel prefix per test). New: `test_sync.py`, `test_protocol_frames.py`, `test_ws_replay.py`, `test_ws_flow.py`, `test_ws_presence.py`, `test_presence_store.py`, `test_ws_access.py`, `test_ws_stress.py` (24 clients, random drops, 4 writers: every client applies exactly `1..event_seq` once in order), `test_client_e2e.py`, `test_manual_part5_sequence.py`. Intervals are configurable and shrunk in tests; waits are wait-until-condition helpers, not fixed sleeps; fault injection (`hub.suppress_hints`, monkeypatched database functions, a failing presence store) is test-only. Client: `cd client && npm test`. Lint: `uv run ruff check . && uv run ruff format --check .` and `npx tsc --noEmit`.

### Part 5 limitations, handbook deviations and unverified assumptions
* **Verification at completion:** `cd backend && uv run pytest`: **961 passed** (real PostgreSQL 16 and Redis 7 in Docker); `cd client && npm test`: **61 passed**; `ruff check`, `ruff format --check`, `tsc --noEmit` and `alembic check` are clean. The realtime suites were repeated three times to look for flakes: the only one found was an environment effect (the Redis container's clock stepping back, see below), fixed in the tests by polling.
* **Flaws found by the end-to-end pass and fixed:** (1) a removed member could still be sent their own `member_removed` frame through a cached access verdict: a hint now invalidates the cache; (2) a refused handshake is invisible to a browser (close 1006), so a removed or signed-out client would retry forever: it now re-bootstraps over HTTP, where 401/404 are definitive; (3) the client waited for `sync_complete` before processing, which deadlocks against the server's ack window on a long replay: batches now run during replay; (4) after a bootstrap raised the cursor the client could acknowledge more than the server had sent (a 4400): acks are capped to what the socket delivered; (5) a hung (not failing) database would have stalled connections past the grace period: every call has a timeout; (6) a presence snapshot could go out unfiltered if the member lookup failed: nothing is sent instead; (7) presence bookkeeping for rooms nobody is in is pruned; (8) the demonstration script did not reconnect A after restarting A's API process.
* **Lost-hint delay: up to one reconcile interval** (default 5 s, 2 s while Redis is unhealthy).
* **Access-loss detection: up to the check interval** (default 15 s) when no hint arrives (logout, expiry, disabled account, a lost removal hint). A removal normally closes the socket within a moment.
* **In-flight frames cannot be recalled** (the page being written when access is revoked).
* **Availability is per room, per user** (not global); display only.
* **Hidden-tab timer throttling** in browsers can delay heartbeats and cause a reconnect; the cursor makes it safe. Not verified against current browser versions.
* **No event pruning** (`room_events` grows without bound), so `too_far_behind` triggers only on `REPLAY_MAX_EVENTS`; the resync path exists so retention can be added.
* The live path does one page read per woken connection (no shared per-room event cache): see the load notes.
* `hub.suppress_hints` is test-only fault injection that lives in production code (default off; not configurable).
* **Handbook deviations (deliberate):** no event pruning (the handbook keeps 7 days of replay events); snapshot-style presence frames (a full list, not deltas); custom close codes (4400–4429) and the 1008 → 4400 change.
* **Unverified assumptions:**
  * Real Google login and **real browsers**: the backend was tested with the `websockets` library and the client with Node + `ws` (and fakes). A browser cannot read the HTTP status of a rejected handshake; the client handles that by re-bootstrapping.
  * A reverse proxy in front of the API (WebSocket upgrade, `Origin` pass-through, idle timeouts versus the ping/heartbeat intervals) was not exercised.
  * **Redis Cluster**: the presence scripts touch two keys of one room (`…:conns` and `…:avail`) that do not share a hash tag, so they assume a single Redis instance (the supported deployment). `rediss://` with a password was validated in configuration only; Valkey and managed Redis were not tested.
  * **Wall clocks**: presence expiry uses Redis `TIME` (wall clock). A clock step on the Redis host can delay or hasten an expiry by the size of the step. **Measured on this development VM:** while 2.2 s of real time passed, the Redis container's clock advanced only about 0.65 s in some iterations (a step of roughly 1.5 s; the same instability seen with PostgreSQL's `clock_timestamp()` in Part 3), which is why the expiry tests poll instead of sleeping a fixed time. Redis's own key TTLs use the same clock, so this cannot be avoided in the application.
  * Scripting facts (atomicity, `TIME` followed by writes with effects replication, keys in `KEYS`, `SCRIPT FLUSH` recovery) come from the Redis documentation, and the flush recovery is covered by a test. The PostgreSQL isolation facts come from its documentation and tests. I could not fetch the Starlette WebSocket documentation site; its behaviour was read from the installed source and confirmed by tests.
  * Development and tests ran on Windows with Docker Desktop; Linux signal handling was not run. Scale was tested up to a few dozen concurrent connections on one machine, not thousands.

---

## Part 6: documents (in progress: checkpoint 2 of 9, frontend shell)

Progress, verification status and the next step live in [docs/documents-progress.md](docs/documents-progress.md);
the document format is [docs/document-schema.md](docs/document-schema.md).

### What exists after checkpoint 1

* **Document model** (`backend/app/documents/`): one validator/canonicalizer in Python and its
  TypeScript twin (`client/src/document-schema.ts`), both run against 76 golden files in
  `shared/fixtures/`; deterministic canonical JSON and SHA-256 `content_hash`; five server-defined
  templates.
* **Tables** (migration `0005`): `documents`, `room_assets`, `document_asset_refs`,
  `document_write_buckets`, plus `rooms.active_document_id` / `active_document_version` and four new
  room event types. Tables for imports and cloud connections arrive with the checkpoints that use
  them.
* **API** (`backend/app/api/documents.py`, `backend/app/services/documents.py`): create, list, read,
  rename, choose the active document, save content.
* **Realtime**: `document_created`, `document_renamed`, `active_document_changed`,
  `document_content_updated` events; `GET /rooms/{id}/sync` and `GET /rooms/{id}` carry
  `active_document_id` / `active_document_version`; the Part 5 client folds document events into
  coalesced `DocumentSignals` and moves its cursor only after the application handled them.

### Routes

| Route | Who | Notes |
|---|---|---|
| `POST /rooms/{room}/documents` | owner | body `title`, `template_key`, `client_request_id`; 201 new, 200 matching retry |
| `GET /rooms/{room}/documents` | member | keyset pagination, metadata only (never content), `is_active`, `requires_review` |
| `GET /rooms/{room}/documents/{doc}` | member | content, versions, `can_edit`, `ETag`; `If-None-Match` gives 304 |
| `PATCH /rooms/{room}/documents/{doc}` | owner | `title` only; bumps `metadata_version` only |
| `PUT /rooms/{room}/active-document` | owner | `document_id`, optional `expected_selection_version` |
| `PUT /rooms/{room}/documents/{doc}/content` | owner, editor | `schema_version`, `expected_content_version`, `content`; up to 2 MiB |

Gate order on every route: body-size limit (413), authentication (401), CSRF (403), unlocked
membership/role pre-check (404 `room_not_found` / 403 `insufficient_role`), validation (422), then a
transaction that locks the room and **rechecks** membership and role. A document, asset or import is
looked up by room **and** id, so another room's id is simply `document_not_found`.

### Error codes added

| Status | Code | Meaning |
|---|---|---|
| 404 | `document_not_found` | no such document in this room |
| 403 | `insufficient_role` | active member without the needed role |
| 409 | `document_version_conflict` | stale `expected_content_version`; carries `current_content_version`; returned even when the content is identical |
| 409 | `document_request_id_reused` | same `client_request_id`, different title/template |
| 409 | `document_limit_reached` | room already has `MAX_DOCUMENTS_PER_ROOM` documents |
| 409 | `selection_conflict` | stale `expected_selection_version`; carries `current_selection_version` |
| 422 | `invalid_document_content` | `error.errors: [{path, code, message}]`, a JSON Pointer path |
| 422 | `asset_not_found` | the content references an asset that is missing, expired or in another room; `error.asset_ids` |
| 413 | `payload_too_large` | over the route's limit (2 MiB for content saves, 128 KiB elsewhere), counted on received bytes |
| 429 | `rate_limited` | `DOCUMENT_WRITES_PER_MINUTE` per user per room; `Retry-After` set |

(`unsupported_media_type`, `quota_exceeded`, `import_*`, `provider_*` arrive with assets, imports and Drive.)

### The save, step by step (`services/documents.save_content`)

Outside any lock: size limit, CSRF, pre-check, JSON parse, **validate and canonicalize**, hash,
collect referenced asset ids. Then one transaction:

1. lock the room (`FOR NO KEY UPDATE`), recheck membership and role;
2. load the document `FOR UPDATE`, scoped to the room;
3. `expected_content_version` differs: 409 (even if the text is identical);
4. canonical hash equals the stored hash: return the current state with `unchanged: true` (no
   version bump, no event, no rate-limit use);
5. referenced assets exist in this room and are not expired, else 422 (asset rows locked);
6. consume the rate-limit bucket (one `INSERT … ON CONFLICT DO UPDATE … WHERE count < limit`);
7. update content, hash, timestamps, `content_version + 1`; maintain `document_asset_refs`; record
   `document_content_updated`; commit.

Every step that can refuse is before every step that writes, and all writes share one transaction, so
a conflict, a rate limit or a crash leaves nothing behind (a test injects a failure after the event
insert and checks that the version, content, refs, bucket and outbox are unchanged). There is no
force option and no merge.

**Lock order:** room, then document, then asset rows, then the rate-limit bucket. Because every
document write takes the room lock first, writes in one room are serialized, which is what makes
"exactly one of two concurrent saves wins" a database property.

### Creation, retries and the active document

`POST …/documents` is one transaction: lock room, recheck owner, look up `(room, user,
client_request_id)`; a match with the same HMAC fingerprint returns 200 with the existing document
and changes **nothing** (it does not re-activate it, even if another document is active now); a
different fingerprint is 409; otherwise the per-room cap is checked, the document inserted, the
room's `active_document_id` set (`active_document_version + 1`; `rooms.version` and `updated_at`
untouched) and `document_created` then `active_document_changed` recorded. The composite foreign key
`rooms (id, active_document_id) → documents (room_id, id)` makes a foreign-room active document
impossible; PostgreSQL's default `MATCH SIMPLE` skips the check when `active_document_id` is NULL
(verified in `tests/test_documents_constraints.py`).

### Migration order (`0005`)

Create `documents` with `UNIQUE (room_id, id)`, add the nullable `rooms` columns, then add the
composite foreign key (it needs the unique key and the columns to exist). Existing rooms get NULL and
version 0; no events are invented. **Downgrade refuses** if document events exist (events are
append-only and gapless) instead of deleting them.

### Configuration added (checkpoint 1)

`DOCUMENT_MAX_SERIALIZED_BYTES`, `DOCUMENT_MAX_TEXT_CODEPOINTS`, `DOCUMENT_MAX_TOTAL_NODES`,
`DOCUMENT_MAX_BLOCKS`, `DOCUMENT_MAX_INLINE_NODES`, `DOCUMENT_MAX_IMAGES`, `DOCUMENT_MAX_LIST_DEPTH`,
`DOCUMENT_MAX_LINK_LENGTH`, `DOCUMENT_FONT_KEYS`, `MAX_DOCUMENTS_PER_ROOM` (200),
`DOCUMENT_WRITES_PER_MINUTE` (120), `DOCUMENT_SAVE_BODY_LIMIT_BYTES` (2 MiB; must be at least the
serialized limit). All validated at startup; see `.env.example`.

### Frontend (checkpoint 2): `frontend/`

React 18 + TypeScript (strict) + Vite, tests with vitest and Playwright. It reuses the framework-free
room client from `client/` (aliased as `threadforge-client`), so there is exactly one event client:
chat and document events share one WebSocket.

```
cd backend && uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log
cd backend && uv run python -m app.workers.outbox_dispatcher        # publishes the live hints
cd frontend && npm install && npm run dev                           # http://localhost:5173
```

The dev server proxies `/auth`, `/rooms`, `/capabilities` and `/health` to the API
(`THREADFORGE_API`, default `http://127.0.0.1:8000`), so the browser sees ONE origin: the session
cookie is first-party and there is no CORS. Add `http://localhost:5173` to the backend's
`TRUSTED_ORIGINS` (CSRF and WebSocket Origin checks compare it exactly). Routes are `#/hash` routes,
so no page path can collide with an API prefix.

What the layout does: navigation (documents) | document area | chat. An empty room shows the owner
**Start document** and **Open document** and everyone else a clear empty state. The start dialog has
a title and five templates, blocks double submits, and a retry after a failure re-sends the SAME
`client_request_id`. The open dialog lists saved documents (owners can *Make active*); Google Drive is
shown as unavailable, with a setup explanation, when `GET /capabilities` says `not_configured`; no
Dropbox/OneDrive UI exists. "Upload from this device" and the Drive picker are NOT shown until their
backend exists. The document area currently renders the content **read-only** (a React renderer that
creates only text nodes and attributes, never `innerHTML`); the editor replaces it for owners and
editors in checkpoint 3 onward. A clean client follows `active_document_changed` and opens the new
active document; an open document is refetched with `If-None-Match` at most once per second after a
newer `document_content_updated`.

`GET /capabilities` (authenticated) answers `{"google_drive": "available" | "not_configured"}` and
nothing else: no keys, ids or secrets. Drive is `available` only when all five Drive settings are
set and none is a placeholder.

**Browser tests** (`frontend/e2e/`, `npm run e2e`): Playwright starts the real API (port 8100), the
outbox dispatcher and Vite against a dedicated database `threadforge_e2e` (created, migrated and
emptied by `backend/scripts/e2e_prepare.py`, which refuses any other name) and Redis database 14.
Logged-in browsers are made by `backend/scripts/seed_session.py`, which writes a user and a session
through the real session service and prints the cookie; Playwright sets that cookie. There is **no
impersonation endpoint** in the API.

### Known limitation (by design in this part)

There is no collaborative merge. Two editors changing the same document will hit version conflicts;
the design makes them early and lossless (a stale save is refused, nothing is overwritten, the
losing client keeps its text) but does not remove them.

### Not built (and no UI will show them)

Simultaneous collaborative editing, revision history, comments, tracked changes, DOCX/PDF export,
AI, document deletion, other cloud providers, merged/nested table cells, headers/footers,
pagination, formatted paste, mobile editing, antivirus scanning.

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
backend/app/        main.py config.py db.py middleware.py body_limit.py request_body.py errors.py
                    pagination.py logging_config.py
  api/              health.py auth.py rooms.py members.py invitations.py messages.py
                    room_events.py (Part 4: history)  realtime.py (Part 4: WebSocket route)
  services/         oidc.py login_attempts.py accounts.py sessions.py csrf.py cookies.py security.py
                    rooms.py memberships.py invitations.py   (Part 2: ALL room/membership writes)
                    messages.py                              (Part 3: ALL message writes)
                    room_lock.py room_events.py outbox.py    (Part 4: lock handle, events, outbox queries)
                    realtime_access.py                       (Part 4/5: read-only WebSocket DB reads)
                    sync.py                                  (Part 5: the bootstrap snapshot)
  realtime/         channels.py publisher.py notifications.py subscriptions.py protocol.py
                    room_hub.py connection.py        (Part 5: hints, reconciliation, access, 4 tasks)
                    presence.py presence_hub.py      (Part 5: Redis-only presence and typing)
  workers/          outbox_dispatcher.py                     (Part 4: standalone process)
  dependencies/     auth.py (CurrentUser, require_csrf)  rooms.py (member/owner pre-checks)
  models/ schemas/  commands/cleanup_auth.py outbox_status.py outbox_requeue.py
backend/migrations/ Alembic (async template), versions/0001_auth_foundation.py, 0002_rooms_...py,
                    0003_messages.py, 0004_room_events_outbox.py
backend/scripts/    manual_part2_check.py ... manual_part4_check.py, manual_part5_check.py
client/             Part 5: TypeScript room client (src/, test/ with vitest, demo/)
backend/tests/      pytest suite (fake Google in tests/fake_google.py; real PostgreSQL and Redis)
compose.yaml        PostgreSQL 16 and Redis 7
.env.example
```
