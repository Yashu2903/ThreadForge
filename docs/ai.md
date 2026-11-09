# ThreadForge AI (phase 1)

This document grows with each checkpoint. **Checkpoint 1** covers providers, keys, consent, switches,
`can_write`, the spend ledger, budgets, caps, the usage page and operator commands (sections 2-8).
**Checkpoint 2 (this version)** adds sources: device uploads, the sandboxed inspector, quotas, removal
and cleanup (section 9). Analysis, retrieval, `@AI` requests and the evaluation come in checkpoints 3 to 5
and are listed under "Not built yet". Progress log: `docs/ai-progress.md`.

## 1. What exists in the repository (inspection result)

The brief says "reuse" for several things. What the repository actually has:

| Brief says reuse | Reality | Adaptation |
|---|---|---|
| authentication, CSRF, rooms, roles, message service, room events, Part 5 client | exists | reused as is |
| claim-token job pattern (outbox dispatcher) | exists (`workers/outbox_dispatcher.py`) | used for the AI worker in checkpoints 3-4 |
| storage adapter, image re-encoding (`assets_pipeline.py`), sandboxed conversion with rlimits | storage and image pipeline exist; **no sandboxed child process, no converter** | checkpoint 2 built the child-process sandbox (`app/sources/sandbox.py`); the converter pieces (text extraction, DOCX heading paths) are checkpoint 3 |
| import jobs, worker, report, publication | **never built** (they were in the old Part 6 plan, replaced by the Tiptap direction) | not claimed; the upload path in checkpoint 2 is built directly (intent / bytes / finalize), without a job queue because inspection is cheap |
| Google Drive connection, Picker, import code | **never built**; only the settings (`GOOGLE_DRIVE_*`) and the `/capabilities` flag exist | Drive as a source stays blocked until a Drive connection exists; the device and room-source paths are built |
| AES-GCM key-set helper "used for Drive tokens" | **does not exist** (only the `CLOUD_TOKEN_ENCRYPTION_KEYS` setting) | `app/ai/crypto.py` is a new, generic key-set helper; AI uses its own setting `AI_KEY_ENCRYPTION_KEYS` |
| collaboration service and its internal API | service, internal API, the Tiptap browser editor and live access enforcement all exist now (collaboration checkpoints 1-6), including the read/apply hooks for AI | document capture (checkpoint 4 of this phase) uses the hooks' `read`; "unsynced changes" detection uses the Tiptap provider |
| rooms "archived" | no such state exists | the `can_write` clause is documented and has nothing to check yet |
| pgvector image | `compose.yaml` uses `postgres:16` | switched in checkpoint 3 when vectors first appear |

## 2. Checkpoint 1: how it works (simple terms)

**Provider** = an outside company's API. We use two: OpenAI for text generation (`gpt-6-luna`) and Google for
embeddings (`gemini-embedding-2`). An **embedding** is a list of numbers that represents the meaning of a
piece of text or an image, so "similar meaning" becomes "nearby numbers". **Token** = a chunk of text
(roughly three quarters of a word) and the unit providers bill in. Everything about a provider (model id,
limits, prices) is a setting; leaving one out leaves AI off with a clear status.

### Files that matter, in reading order

1. `backend/app/ai/types.py` - the two adapter interfaces (`LLMProvider`, `EmbeddingProvider`).
2. `backend/app/ai/providers/openai_llm.py`, `google_embed.py` - the real HTTP code for each provider.
3. `backend/app/ai/errors.py`, `http.py` - error classes and key scrubbing; one HTTP wrapper, no redirects.
4. `backend/app/ai/calls.py` - **the call policy**: every provider call goes through `MeteredCaller.call`.
5. `backend/app/ai/spend.py` - the ledger and the budget reservation (the guarded upsert).
6. `backend/app/ai/crypto.py`, `keys.py` - encrypted personal keys and the key-source decision.
7. `backend/app/services/permissions.py` - `decide_write`, the one `can_write`.
8. `backend/app/ai/access.py` - the admission gate (every "may this user use AI here?" question).
9. `backend/app/ai/selfcheck.py`, `status.py` - capability checks and `GET /ai/status`.
10. `backend/app/api/ai.py` - the routes. `backend/app/commands/ai_*.py` - operator commands.

### The path of one provider call (`MeteredCaller.call`)

```
caller ──► per-request caps (calls, input/output tokens, images)   refuse before sending
       ──► cancelled?                                              record "cancelled", stop
       ──► reserve worst-case cost against the budgets             (server key only)  one transaction
       ──► per-key concurrency slot ──► HTTP call (no DB transaction is open here)
                 ok        ──► ledger row (real usage) ──► settle reservation
                 429/5xx/timeout ──► ledger row "failed" ──► wait (backoff + jitter or Retry-After) ──► again
                 400/401/403/refusal/bad output ──► ledger row "failed" ──► release reservation ──► raise
```

* **Retry policy**: retry 429 (honouring `Retry-After`, capped at 120 s), 5xx and timeouts, up to
  `AI_RETRY_MAX_ATTEMPTS` (4), exponential backoff capped at `AI_RETRY_MAX_DELAY_SECONDS`, 50-100 % jitter.
  Never retry 400, 401, 403, refusals or malformed output. We use plain HTTP and not the vendor SDKs, because
  SDKs retry silently, which would hide spend from the ledger.
* **Transactions**: a ledger row, a reservation, a settle each use their own short transaction. No
  transaction is held open while an HTTP call is in flight.
* **Scrubbing**: provider error text goes through `scrub()` (known secrets, `sk-...`, `AIza...`,
  `Bearer ...`, any 32+ character token) and is truncated to 200 characters before it is stored or shown.
* **Structured output**: with `AI_LLM_STRUCTURED_OUTPUT=true` the request sets `text.format` to a strict JSON
  schema (constrained decoding). With `false` the schema is put in the instructions, the reply is parsed,
  and **one** repair call is made on a parse failure. The model gets no tools and no network.
* **Token counting**: no verified local tokenizer, so `ceil(chars / 3.0) * 1.1`. It over-counts on purpose.
  Provider-reported usage replaces it in the ledger; Google reports none for embeddings, so those rows are
  flagged `usage_estimated`.

### Spend controls

* **Ledger** (`ai_usage_ledger`, append-only, one row per call attempt, including failed and retried and
  cancelled ones): integer micro-dollars and tokens, `key_source` (server/user), status, safe error code.
  `user_id`/`charged_user_id` are NULL only for the operator self-check.
* **Budgets** (server key only): global per month, per user per day, per room per day, in
  `ai_budget_windows`. **Reservation** = before a call, set aside its worst-case cost; afterwards `settle`
  (replace by real cost) or `release`. The atomic step is one guarded `INSERT ... ON CONFLICT DO UPDATE ...
  WHERE spent + reserved + est <= limit RETURNING`. PostgreSQL locks the row for the update, so two
  concurrent callers queue and the second sees the first's reservation. One transaction takes the global,
  user and room windows in a fixed order; a refusal anywhere rolls back the rest. Test:
  12 concurrent reservations of 30 against a budget of 100 grant exactly 3.
  Overshoot is possible only if a call costs more than its worst-case estimate (it cannot unless prices
  are configured too low).
* **Warnings**: at 50, 80 and 100 % of spent, once per window (`ai_budget_warnings` primary key), logged and
  shown on the owner's usage page. At 100 % new server-key calls stop with `429 budget_exhausted`.
* **Caps** per request (`RequestMeter`): provider calls, input tokens, output tokens, images. New-analysis
  page/figure caps are settings now and are enforced when analysis exists (checkpoint 3).
* **Estimate** (`POST /rooms/{id}/ai-requests/estimate`): see section 4. The send route that enforces
  `confirm_large` (`409 confirmation_required`) is built in checkpoint 4; `needs_confirmation()` is ready.
* **If prices are unset**: costs are 0, budgets cannot be configured (startup error), only token caps apply,
  and `GET /ai/status` says `budgets_enforced: false`.

### Keys (bring your own key)

Policy `AI_KEY_POLICY`: `server_only`, `user_only`, `user_if_present` (default: your key if saved, else the
server's). Decided **per provider at admission** and recorded with the request. At execution a missing key
is `key_unavailable`, a rejected one `key_rejected`; the system **never** switches to the other source.

* Encryption: AES-256-GCM, 12-byte random nonce, key set from `AI_KEY_ENCRYPTION_KEYS`
  (`id:base64(32 bytes)`, first key encrypts, older ones still decrypt, so rotation works).
  **AAD** (data authenticated but not stored) = `user_id` + `provider`, so a ciphertext copied to another
  user or provider fails to decrypt (tested).
* Saving: rate-limit first (per user and per IP per hour, counted whatever the outcome, committed before the
  provider is called), then ONE free metadata call (`GET /v1/models/{model}` or
  `GET /v1beta/models/{model}`) with the key, then encrypt and upsert. The plaintext exists only in local
  variables. Never logged, returned, or stored; the API returns provider, last4, state and dates.
* Only workers decrypt (`keys.load_api_key`). A worker that sees a 401/403 can call `mark_rejected`.

## 3. Permissions

`decide_write(user, document)` in `services/permissions.py` is the only definition of `can_write`: user
active, active member, role owner/editor, document in that room, `collab_state != needs_repair`, not
size-locked (stored Yjs state >= `COLLAB_MAX_STATE_BYTES`, where the collaboration service is already
read-only), room not archived (no such state yet). Two callers:

* the collaboration service's authorize endpoint uses `role_ok` (membership + role). It deliberately does
  **not** apply the `needs_repair` and size clauses, because people must still be able to edit a
  `needs_repair` document (that is how it heals). This is the one place the shared function is used with a
  narrower question, and it is tested (`test_collab_authorize_uses_the_same_function`).
* every AI path uses `allowed` (all clauses), at admission now and in the worker (claim, retry, before
  publish) in checkpoint 4.

Gate order (`ai/access.py`), each with its own code: role (viewer: `403 insufficient_role`), configuration
(`503 ai_not_configured`), global kill switch (`503 ai_disabled`), failed required capability
(`503 ai_unavailable`), room switch (`409 ai_disabled_for_room`), anchor document
(`409 ai_anchor_required`; another room's document is `404 document_not_found`), `can_write`
(`403 ai_write_access_required`), consent (`403 consent_required`). Outsiders and removed members get
`404 room_not_found` from the route dependency before any of this.

| Action (checkpoint 1) | Owner | Editor | Viewer | Outsider/removed |
|---|---|---|---|---|
| estimate / (later) invoke `@AI` | yes, with `can_write` | yes, with `can_write` | 403 | 404 |
| read the room AI switch | yes | yes | yes | 404 |
| change the room AI switch, view usage | yes | 403 | 403 | 404 |
| save/delete own keys, consent, status | any signed-in user, own account only | | | |

The global kill switch is `ai_global_flags` (one row) changed with `python -m app.commands.ai_disable` /
`ai_enable`; effect on the next request, no redeploy. A missing row means "on".

### Consent

`GET /ai/consent` returns the current `notice_version`, the points (what is sent where), whether you
accepted, and which key each provider would use. `POST /ai/consent {notice_version}` records
`(user_id, notice_version, accepted_at)`; a different version is `409 notice_outdated`. Bumping
`NOTICE_VERSION` in `ai/notice.py` makes everyone accept again.

Notice text (version 1):

1. When a file you attach is analyzed for the first time, its text and images are sent to the embedding provider (Google) so they can be searched.
2. To answer, the passages retrieved from your files, any images selected for the answer, the document text you include, and any chat messages you include are sent to the generation provider (OpenAI).
3. Scanned pages that local text recognition cannot read are sent to the generation provider's vision model to be transcribed.
4. Requests use either your own API key or the server's key. You will be told which one before you send.
5. Other members of the room can already read the files you attach; AI answers are visible to everyone in the room.
6. Answers can be wrong. A citation shows which passage the answer used, not that the passage proves the claim.

## 4. API reference (checkpoint 1)

All responses `Cache-Control: no-store`; errors use the standard envelope with lowercase `snake_case` codes.

| Route | Auth | Notes |
|---|---|---|
| `GET /ai/status` | session | booleans and codes only: `available`, `code`, per-capability `state` (`ok`/`failed`/`not_checked`) and safe code, `spend_controls` |
| `GET /ai/consent`, `POST /ai/consent` | session / +CSRF | see above |
| `GET /ai/keys` | session | metadata only |
| `PUT /ai/keys/{openai\|google}` | CSRF | `{api_key}`; 200 metadata; 422 `key_rejected`/`model_unavailable`/validation; 429 rate limit; 502 `provider_unavailable`; 409 `user_keys_not_allowed`; 503 `ai_not_configured` |
| `DELETE /ai/keys/{provider}` | CSRF | 204 |
| `GET /rooms/{id}/ai-settings` | member | `{ai_enabled, available, code}` |
| `PUT /rooms/{id}/ai-settings` | owner, CSRF | `{ai_enabled}`; emits `room_ai_settings_changed` only on a real change |
| `GET /rooms/{id}/ai-usage?days=` | owner | totals, by day / user / operation / key source for this room, budget windows, warnings |
| `POST /rooms/{id}/ai-requests/estimate` | member, CSRF | gate (without consent) then estimate: tokens, calls, expected and worst-case cost, `requires_confirmation`, `over_caps`, key source per provider, `consent_given`, `can_send`, `blocking_code` |

Error codes added: `ai_not_configured`, `ai_disabled`, `ai_unavailable`, `ai_disabled_for_room`,
`consent_required`, `ai_write_access_required`, `ai_anchor_required`, `key_unavailable`, `key_rejected`,
`model_unavailable`, `provider_unavailable`, `user_keys_not_allowed`, `notice_outdated`, `budget_exhausted`
(429), `rate_limited` (429, `Retry-After`).

**Events**: `room_ai_settings_changed` (resource type `room`, payload `{ai_enabled}`), recorded in the same
transaction as the change, under the room lock, with its outbox row. The existing Part 5 client advances its
cursor past event types it does not handle; the UI reaction is built with the AI UI in checkpoint 4.

## 5. Self-check

`python -m app.workers.ai_worker` (at start and every `AI_SELFCHECK_INTERVAL_SECONDS`) or
`python -m app.commands.ai_selfcheck` makes small real calls with the **server** keys: text generation, a
schema-valid typed reply, an image (a solid-colour picture's colour), a text embedding (exact dimension), an image
embedding (a picture ranks higher with its matching text than with the mismatched one, both ways), and
`tesseract --list-langs` for `eng`. Results are in `ai_capability_status`. Required for AI: text generation,
structured output, text embedding (a failure turns AI off with `ai_unavailable`). Image input, image embedding
and Tesseract only disable their own feature. A missing server key is `not_checked`, never `ok`.
The worker in checkpoint 1 does the self-check only; the job-claim loop is added in checkpoints 3-4.

## 6. Settings

See `.env.example` (section "ThreadForge AI"). Validation at startup names the setting. Highlights: with
`AI_ENABLED=true` the models, context/output/dimension limits are required; `AI_PRICES_CHECKED_ON` is
required if any price is set; budgets require prices; `server_only` requires both server keys; any policy
that lets users bring keys requires `AI_KEY_ENCRYPTION_KEYS`; `AI_MAX_INPUT_TOKENS + AI_MAX_OUTPUT_TOKENS`
must fit the context window.

## 7. Values to confirm (read from the official documentation on 2026-11-08)

AI stays disabled until you set `AI_ENABLED=true`. The owner's blanks were filled from the provider docs;
please confirm each before enabling.

| Setting | Proposed | Source / status |
|---|---|---|
| Generation model | `gpt-6-luna` ("GPT-6 Luna") | OpenAI models page: efficient model for focused, high-volume tasks |
| Context window / max output | 1,050,000 / 128,000 tokens | same page |
| Structured output | enabled (`AI_LLM_STRUCTURED_OUTPUT=true`) | the page does **not** list it per model; the self-check proves it |
| Image input | yes (PNG, JPEG, WEBP, non-animated GIF; up to 1,500 images / 512 MB per request) | images guide: "all latest OpenAI models support text and image input" |
| Generation price | USD 0.10 in / 0.50 out per 1M tokens | models page, checked 2026-11-08 |
| Embedding model | `gemini-embedding-2` ("Gemini Embedding 2") | Gemini embeddings guide |
| Inputs | text (8,192 tokens), images PNG/JPEG (up to 6 per request), interleaved parts give one aggregated embedding | same |
| Dimensions | default 3072, allowed 128-3072; **proposed 1536** (half the storage; recommended value) | same |
| Task types | none (no `task_type`); use text prefixes (`task: search result \| query:` / `title: none \| text:`) | same; both prefixes are settings |
| Re-normalization | not needed for this model (it normalizes shortened vectors) | same; `AI_EMBED_NORMALIZE` is available if the self-check disagrees |
| Embedding price | text USD 0.20, image USD 0.45 per 1M tokens (batch is half) | pricing page, checked 2026-11-08 |

## 8. Unverified assumptions

These are parsed defensively and decided by the real-provider self-check, which has **not been run** (no
credentials were used in development; all provider behaviour in tests is an HTTP-level stub):

* OpenAI Responses API response fields `usage.input_tokens` / `usage.output_tokens` (if absent, usage is
  estimated and flagged), the `reasoning.effort` parameter placement, `GET /v1/models/{id}` as the free key
  check, and that a rejected key is HTTP 401/403.
* Which JSON Schema keywords strict structured output supports (the page was not read past its first 100,000
  characters). The self-check uses a deliberately simple schema.
* Gemini REST details: the batch response key (`embeddings[].values`), the `inline_data` / `mime_type`
  spelling for images, the `output_dimensionality` field spelling in REST, that an invalid key is HTTP 400
  `API_KEY_INVALID`, `GET /v1beta/models/{id}` as the free key check, and that no usage metadata is returned.
* That a solid red/blue picture ranks with its matching text under `gemini-embedding-2` (the image-embedding
  self-check; if it fails, only figure embedding by image is disabled).
* Token estimation has no tokenizer match (conservative estimate instead).

### Added by checkpoint 2

* The Linux resource limits were verified by hand in a `python:3.12-slim` container (limits applied; a
  600 MiB allocation fails under a 256 MiB limit; a busy loop is killed at its CPU limit; writes fail with
  `EFBIG`; sockets are refused). The automated tests ran on **Windows**, where only the timeout,
  environment and output-cap parts are exercised; the POSIX-only test (`rlimits` is true) is skipped there
  and has **not run in CI** (there is none). Run `pytest tests/test_sources_inspect.py` on Linux.
* PDFium (`pypdfium2` 5.15.1, BSD-3-Clause / Apache-2.0) on hostile PDFs beyond the fixtures here
  (truncated, encrypted, garbage) is not fuzzed. A malicious PDF could still exploit PDFium; the child
  process, its limits and "a crash means refused" are the containment, not a guarantee.
* "Likely scanned" is a heuristic (no text layer but something drawn). A drawing-only vector page counts
  as scanned; a scan with an invisible OCR text layer counts as text.
* The 200:1 ratio and 200 MiB expansion rules for DOCX are conservative choices, not standards.
* Browser behaviour for a `PUT` of a `File` body (progress, abort, retries) was not exercised: there is no
  UI yet.

## 9. Checkpoint 2: sources

A **source** is a file in a room (PDF, DOCX, plain text or Markdown) that an AI request can later use as
evidence. This checkpoint stores files and looks at them *cheaply*; it never reads their text for meaning,
never runs OCR, never creates embeddings and never calls a provider. Terms used below:

* **Storage key**: a random 32-character name for a stored file (`app/storage.py`). The user's file name
  is never part of a path, so no name can point outside the storage folder.
* **Signature**: the first bytes of a file (`%PDF-` for PDF, `PK\x03\x04` for ZIP, which DOCX is). A file
  called `.pdf` whose first bytes say ZIP is refused no matter what it is called.
* **Child process / sandbox**: a separate program started just for one job, with almost no environment,
  an empty working folder, a time limit and (on Linux) memory/CPU/file limits. If a hostile PDF crashes or
  takes over the PDF reader, it happens in that child, not in the API.
* **Idempotency key** (`client_request_id`): the browser's own id for one upload attempt. Sending the same
  id again returns the same upload instead of making a second one.
* **Immutable version**: replacing a file never overwrites anything; it adds version 2. A request that
  pinned version 1 keeps seeing exactly version 1.

### Files that matter, in reading order

1. `backend/migrations/versions/0008_ai_sources.py`, `app/models/source.py` - the three tables.
2. `app/sources/files.py` - names (`sanitize_name`), kinds (`classify`), signatures (`check_signature`),
   the download header. Pure functions.
3. `app/sources/inspect_child.py` - the inspector that runs in the child. Standalone: it imports only the
   standard library and `pypdfium2`, never `app.*`.
4. `app/sources/sandbox.py` - starts the child, reads its one-line answer, enforces the deadline.
5. `app/services/sources.py` - every database write: intent, bytes, finalize, list, remove, quotas.
6. `app/api/sources.py` - the routes. `app/commands/cleanup_ai.py` - cleanup.

### The upload, step by step

```
browser                           API                                          database / disk
-------                           ---                                          ---------------
POST /rooms/{r}/source-uploads    CSRF, role (owner/editor)                    lock room; re-read role
  {client_request_id, filename,   sanitize name, pick the kind from the        same id seen before? -> return it
   media_type?, byte_size,        extension, check declared type               rate limit, open-upload cap, quotas
   source_id?}                                                                 INSERT source_uploads (state intent,
                                                                                 expires in SOURCE_UPLOAD_TTL_MINUTES)
PUT .../{upload}/content          CSRF, role, upload is mine and still open    (no lock while streaming)
  raw file bytes                  stream to tmp/, COUNT bytes, SHA-256,        file moved to sources/<key> (atomic)
                                  count must equal byte_size, signature        lock room; UPDATE -> state uploaded
                                  must match the kind
POST .../{upload}/finalize        CSRF, role                                   (no transaction while the child runs)
                                  child process inspects the file              lock room; role again; quotas again with
                                                                                 the REAL size; lock the source row;
                                                                                 INSERT source (or next version);
                                                                                 UPDATE sources.current_version_id;
                                                                                 UPDATE upload -> finalized
```

* **Transactions.** Each database step is one short transaction that starts by locking the room row and
  re-reading the caller's role under that lock, so a demotion or removal between steps is caught (tested:
  demoted before finalize -> 403 and no source). Streaming, the child process and file deletes never run
  inside a transaction.
* **Failure behaviour.** Wrong byte count or signature, or an inspector verdict of "no", sets the upload to
  `failed` with a stable `error_code`, deletes the bytes at once and returns the error; a failed upload is
  final (start a new one). A client that just disconnects mid-stream leaves the upload `intent`; it can send
  the bytes again until it expires. A crash between "file moved into storage" and "row updated" leaves an
  orphan file that cleanup removes after 24 hours.
* **Idempotency.** Same `client_request_id` + same fields = same upload (200, nothing consumed); same id,
  different fields = `409 source_upload_id_reused`. The comparison uses an HMAC (keyed with the existing
  message-fingerprint secret, domain-separated), not a plain hash. Repeating the byte PUT after success
  returns the stored upload (the response carries `sha256` so a client can compare); repeating finalize
  returns the same source. Two finalize calls at once make exactly one source (row lock + state re-check).
* **Replace** is an intent that names `source_id`. It adds a version under a row lock, so concurrent
  replacements get distinct, gap-free version numbers; the last to commit is current.

### What the inspector measures and refuses

Measured (stored on the version, used for estimates in checkpoint 4): `page_count`, `text_page_count`
(pages with at least 10 non-space characters of text layer), `likely_scanned_pages` (no text layer but
something drawn, usually a picture), `image_count`, `text_chars`. DOCX has no real pages, so those three
are NULL; its `image_count` is the number of `word/media/*` parts and `text_chars` the characters in `w:t`
runs. Nothing is extracted or stored beyond these numbers.

| Code (`422 invalid_source`, field `reason`) | When |
|---|---|
| `pdf_encrypted` | password-protected, or encrypted at all (even owner-password-only) |
| `pdf_invalid` | PDFium cannot open it, or it has no pages |
| `too_many_pages` | more than `SOURCE_MAX_PAGES` (400) |
| `docx_invalid` | not a ZIP; unsafe part names (`..`, absolute, backslash, duplicates); too many parts; expands past `SOURCE_DOCX_MAX_UNCOMPRESSED_BYTES`, or a part over 1 MiB with ratio over 200:1 (zip bomb); `[Content_Types].xml` or `word/document.xml` missing or not well-formed XML; any `DOCTYPE` or entity declaration; main part is not the Word document type |
| `docx_macros` | `vbaProject.bin`, `vbaData.xml`, `word/activeX/`, or any `macroEnabled` content type |
| `docx_encrypted` | an encrypted ZIP entry |
| `text_invalid` | NUL bytes, not UTF-8 (a BOM is fine), empty or whitespace only, 1% or more control characters |
| `inspection_timeout`, `inspection_failed` | the child hit the deadline, crashed, ran out of memory or gave an unusable answer. A file that breaks the inspector is refused, never accepted |
| `415 type_mismatch` / `unsupported_media_type` | extension not accepted; declared type disagrees; first bytes disagree with the extension (a PDF renamed `.txt`, a ZIP renamed `.pdf`) |
| `422 invalid_source`, reason `size_mismatch` | the bytes received differ from the declared size |

**Sandbox limits, honestly.** The child always gets: an empty environment (only what Python needs to start;
no `DATABASE_URL`, no keys), its own empty working directory (removed afterwards), a wall-clock deadline
(`SOURCE_INSPECT_TIMEOUT_SECONDS`, default 30; the parent kills it), a cap on what it may print, and it
refuses to create sockets (Python level). On **Linux/macOS** it also sets address-space
(`SOURCE_INSPECT_MEMORY_MB`, 1024), CPU-seconds, open-file, written-file-size (0) and no-core-dump limits on
itself before opening anything. On **Windows** (development machines) those resource limits do not exist;
the child reports `rlimits: false` and the API logs a warning once. There is **no network namespace**: a
compromised native library could open a socket in spite of the Python-level block, so production should deny
the API container outbound access it does not need. There is no seccomp filter.

### Tables (migration `0008`, expand only)

* `sources` (id, room_id, created_by, display_name, current_version_id, state `active`/`removed`,
  created_at, removed_at, removed_by). `UNIQUE (room_id, id)`.
* `source_versions` (id, source_id, room_id, version_no, original_name, kind, media_type, byte_size, sha256,
  storage_key, analysis_state, the inspection counts, error_code, created_by, created_at, removed_at).
  `UNIQUE (source_id, version_no)`, `UNIQUE (room_id, id)`, composite foreign key `(room_id, source_id)` so a
  version can never sit in another room than its source. `storage_key` is NULL only once removed and its
  file is gone. `analysis_state` is `not_analyzed` for everything in this checkpoint (checkpoint 3 moves
  it through `queued`, `extracting`, `indexing`, `ready`, `failed`).
* `source_uploads` (the intent, the bytes' hash and key, state `intent`/`uploaded`/`finalized`/`failed`/
  `cancelled`/`expired`, the idempotency key and fingerprint, `expires_at`). `UNIQUE (room_id, created_by,
  client_request_id)`. Rows are small and are kept (the idempotency window); they are not pruned.
* `sources.current_version_id` is a deferrable composite foreign key, added after both tables exist.
* Nothing existing is altered. The downgrade drops only these three tables.

### API reference

| Route | Who | Notes |
|---|---|---|
| `GET /rooms/{id}/sources` | any member | active sources with current version and counts, `used_sources`, `used_bytes`, `limits`, `can_upload`, `can_download` |
| `GET /rooms/{id}/sources/{sid}` | any member | the source with all versions. (Extracted passages come with checkpoint 3 and will be owners/editors only.) |
| `POST /rooms/{id}/source-uploads` | owner/editor, CSRF | step 1; `201`, or `200` for a repeated `client_request_id`; include `source_id` to replace |
| `PUT /rooms/{id}/source-uploads/{uid}/content` | creator, CSRF | step 2; body = raw bytes, streamed and counted; limit `SOURCE_MAX_UPLOAD_BYTES` |
| `POST /rooms/{id}/source-uploads/{uid}/finalize` | creator, CSRF | step 3; `201` with the source; repeating returns the same source |
| `GET /rooms/{id}/source-uploads/{uid}` | creator | state, hash, error code |
| `DELETE /rooms/{id}/source-uploads/{uid}` | creator, CSRF | abandon an unfinished upload now; bytes deleted |
| `GET /rooms/{id}/sources/{sid}/versions/{vid}/download` | `SOURCE_DOWNLOAD_ROLES` | original bytes: `attachment`, `nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, `no-store`, sanitized name |
| `DELETE /rooms/{id}/sources/{sid}` | owner any; editor own | `204`; see removal |

Permissions (tested route by route): list/inspect: owner, editor, viewer; upload/replace: owner, editor
(viewer `403 insufficient_role`); an upload belongs to its creator alone (even the owner gets `404`);
remove: owner any source, editor only a source they created (the creator of the source, not of a later
version), viewer never; download: roles in `SOURCE_DOWNLOAD_ROLES` (default all three; the setting must
include `owner`). Outsiders and removed members get `404 room_not_found` before anything else, and a
non-member sending a malformed body still gets `404`, a viewer `403`, not `422`.

Error codes added: `source_not_found`, `source_upload_not_found` (404), `invalid_source` (422, with
`reason`), `source_upload_not_open` (409, with `state`), `source_upload_id_reused` (409),
`too_many_open_uploads` (409), `source_unavailable` (409, lists the version ids; used by checkpoint 4's
admission), `type_mismatch` / `unsupported_media_type` (415), plus the existing `quota_exceeded` (409),
`rate_limited` (429, `Retry-After`), `payload_too_large` (413).

### Quotas and rates

Per room: `SOURCE_MAX_PER_ROOM` (100) active sources and `SOURCE_ROOM_QUOTA_BYTES` (500 MiB) over all live
versions. Uploads in flight **reserve** against both (so two uploads cannot both take the last slot; tested
with parallel requests); a reservation ends when the upload finishes, fails, is cancelled or expires.
Per user per room: `SOURCE_UPLOADS_PER_MINUTE` (10, counted from rows, so retries and refused intents
consume nothing) and `SOURCE_MAX_OPEN_UPLOADS_PER_USER` (3 unfinished at once). Quotas are checked at
the intent with the declared size and again at finalize with the real size.

### Removal

`DELETE` marks the source and **every** version `removed` in one transaction under the room lock, then
deletes the original files and clears the storage keys. If a file cannot be deleted the key stays on the
`removed` row and `cleanup_ai` finishes the job (tested). A removed source disappears from listings and
returns 404 everywhere; its quota is freed at once. Checkpoint 3 adds chunks, embeddings and figure images
to this same step; checkpoint 4 adds redaction of stored excerpts and manifests. **Already-published chat
answers keep their text** (known limit, stated again in the final report).

### Cleanup (`python -m app.commands.cleanup_ai`)

Expires abandoned uploads (open past their `expires_at`, state re-checked under the row lock so a finalize
that got there first wins - tested racing), deletes the files of failed/cancelled/expired uploads and of
removed versions that survived, deletes orphan files in `sources/` that no row names and that are older than
24 h, and removes stale `tmp/inspect-*` directories and `tmp/up-*` files older than an hour. Idempotent;
needs only `DATABASE_URL` and `STORAGE_PATH`. Later checkpoints add generations, requests, manifests and
budget windows to it.

### Settings

`SOURCE_MAX_UPLOAD_BYTES` (25 MiB), `SOURCE_MAX_PER_ROOM`, `SOURCE_ROOM_QUOTA_BYTES`,
`SOURCE_UPLOADS_PER_MINUTE`, `SOURCE_MAX_OPEN_UPLOADS_PER_USER`, `SOURCE_UPLOAD_TTL_MINUTES`,
`SOURCE_DOWNLOAD_ROLES`, `SOURCE_MAX_PAGES`, `SOURCE_INSPECT_TIMEOUT_SECONDS`, `SOURCE_INSPECT_MEMORY_MB`,
`SOURCE_INSPECT_CONCURRENCY`, `SOURCE_DOCX_MAX_UNCOMPRESSED_BYTES`, `SOURCE_DOCX_MAX_ENTRIES`. All validated
at startup with messages that name the setting. They do not depend on `AI_ENABLED`: with AI off, or the
room switch off, or the global kill switch on, uploads still work and files are stored, not analyzed
(tested).

### Decisions and what this checkpoint deliberately does not do

* **Google Drive** is not built (the owner deferred it; there is no Drive connection in the repository).
  The sources tables and routes do not depend on it; a Drive path would produce the same
  intent -> bytes -> finalize result from a server-side download.
* **"From room sources (select existing)"** is the list route plus `resolve_versions()` in
  `services/sources.py`, which checkpoint 4's admission transaction calls to verify that every chosen
  version belongs to the room, is not removed and its source is active. There is no separate route
  because selecting happens when the `@AI` request is sent.
* **No `source_updated` events yet.** The brief lists them with the `@AI` events in checkpoint 4 (event
  type, payload, bootstrap/replay handling and the Part 5 client change together). Until then clients
  re-fetch the list.
* **Passages / "inspect extracted passages"** do not exist yet: there is nothing extracted. The
  route arrives with checkpoint 3.
* **Superseded versions are kept** (their bytes count toward the room quota) until checkpoint 4 can tell
  which are pinned by requests; remove the source to free them.
* **No UI.** The source panel and attachment control are part of the AI UI in checkpoint 4 (no control
  without behaviour).
* Text files must be UTF-8; other encodings are refused rather than guessed.

**New dependencies (licences checked from the installed metadata):** `pypdfium2` 5.15.1 (BSD-3-Clause or
Apache-2.0; it bundles PDFium, Apache-2.0, and not the AGPL PyMuPDF) is a runtime dependency; `pypdf` 7.0.0
(BSD-3-Clause) is a development dependency used only to build encrypted-PDF test files.

## 10. Limits (stated plainly)

* If prices are misconfigured too low, spend control is lost: budgets count what the configuration says.
* Embedding usage is estimated (Google reports none), so embedding spend is approximate.
* A worker crash between a provider call and its ledger row can lose that row (the spend still happened).
* A file removed from a room stays in already-published chat answers (their text is never rewritten).
* Superseded source versions stay stored (and count toward the quota) until the source is removed;
  checkpoint 4 decides which can be dropped once requests pin versions.
* Inspection cannot tell a scan from a drawing, and cannot find an unreadable scan (that is only
  discovered when OCR runs at first use, checkpoint 3).
* Checkpoint 1 builds no UI for keys, consent, the room switch or usage. The pages arrive with the AI UI
  (checkpoint 4); until then the controls exist only as API routes (no control without behaviour).

## 11. Not built yet

Analysis, OCR, figures, embeddings storage, retrieval, the passages inspect route (3), `@AI` requests,
document capture, answers, Mermaid, publication, `source_updated` / `ai_request_updated` events, the source
and AI UI (4), evaluation, security sweep, final report (5). Google Drive as a source depends on a Drive
connection that does not exist in the repository (deferred by the owner).
