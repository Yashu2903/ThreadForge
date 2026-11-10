# ThreadForge AI (phase 1)

This document grows with each checkpoint. **Checkpoint 1** covers providers, keys, consent, switches,
`can_write`, the spend ledger, budgets, caps, the usage page and operator commands (sections 2-8).
**Checkpoint 2** adds sources: device uploads, the sandboxed inspector, quotas, removal and cleanup
(section 9). **Checkpoint 3** adds lazy analysis and retrieval: the analysis queue and
worker, OCR with a vision fallback, figures, chunking, embeddings, published index generations and hybrid
retrieval (section 10). **Checkpoint 4** adds `@AI` requests, document capture, strategies, validated cited
answers, Mermaid diagrams and the chat UI (section 11). **Checkpoint 5** adds the evaluation harness and
its results (section 13), the security and regression sweep (section 14) and the final report (section
15). Progress log: `docs/ai-progress.md`.

## 1. What exists in the repository (inspection result)

The brief says "reuse" for several things. What the repository actually has:

| Brief says reuse | Reality | Adaptation |
|---|---|---|
| authentication, CSRF, rooms, roles, message service, room events, Part 5 client | exists | reused as is |
| claim-token job pattern (outbox dispatcher) | exists (`workers/outbox_dispatcher.py`) | the same idea (token, lease, `SKIP LOCKED`) drives the analysis queue (section 10); requests join it in checkpoint 4 |
| storage adapter, image re-encoding (`assets_pipeline.py`), sandboxed conversion with rlimits | storage and image pipeline exist; **no sandboxed child process, no converter** | checkpoint 2 built the child-process sandbox (`app/sources/sandbox.py`); the converter pieces (text extraction, DOCX heading paths) are checkpoint 3 |
| import jobs, worker, report, publication | **never built** (they were in the old Part 6 plan, replaced by the Tiptap direction) | not claimed; the upload path in checkpoint 2 is built directly (intent / bytes / finalize), without a job queue because inspection is cheap |
| Google Drive connection, Picker, import code | **never built**; only the settings (`GOOGLE_DRIVE_*`) and the `/capabilities` flag exist | Drive as a source stays blocked until a Drive connection exists; the device and room-source paths are built |
| AES-GCM key-set helper "used for Drive tokens" | **does not exist** (only the `CLOUD_TOKEN_ENCRYPTION_KEYS` setting) | `app/ai/crypto.py` is a new, generic key-set helper; AI uses its own setting `AI_KEY_ENCRYPTION_KEYS` |
| collaboration service and its internal API | service, internal API, the Tiptap browser editor and live access enforcement all exist now (collaboration checkpoints 1-6), including the read/apply hooks for AI | document capture (checkpoint 4 of this phase) uses the hooks' `read`; "unsynced changes" detection uses the Tiptap provider |
| rooms "archived" | no such state exists | the `can_write` clause is documented and has nothing to check yet |
| pgvector image | `compose.yaml` used `postgres:16` | checkpoint 3 switched it to `pgvector/pgvector:pg16` (same major version, same data volume) |

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

## 10. Checkpoint 3: lazy analysis and retrieval

Attaching a file to an AI request (or pressing "Analyze now") is what makes the system read it. This
checkpoint is everything between "a file is stored" (checkpoint 2) and "its passages can be searched":
a queue, a worker, a sandboxed extractor, local OCR with a vision fallback, figure descriptions,
chunking, embeddings, an atomically published index, and hybrid retrieval. No `@AI` request exists yet
(checkpoint 4); the API can only ask for analysis, cancel it, show its status and let owners and editors
read the extracted passages.

Terms used below:

* **Job** (`analysis_jobs`): "analyze THIS source version with THIS configuration". The table is the
  **queue**.
* **Claim token and lease**: a worker takes a job by writing a random token and a deadline on it. It
  renews the deadline while it works (a heartbeat every third of the lease) and presents the token for
  every later write. A worker that dies stops renewing; after the deadline another worker takes the job
  (new token, attempts + 1). A worker that was only slow wakes up, finds its token gone, and can no
  longer change anything.
* **`FOR UPDATE SKIP LOCKED`**: "give me a row nobody else has locked". Two workers claiming at once get
  different jobs; neither waits for the other.
* **Waiter** (`analysis_job_waiters`): who is waiting for a job (an `@AI` request in checkpoint 4, or a
  person who pressed Analyze now). Two requests attaching the same file share ONE job (a partial unique
  index makes that true even in a race). When the last waiter leaves, the job is cancelled.
* **Generation** (`index_generations`): one complete index of one source version, built with one
  configuration. **Chunk** (`chunks`): one passage of it (text or figure) with its locator, its
  full-text column and its **embedding**, a list of numbers (a **vector**) that stands for the meaning
  of the passage, so "similar meaning" becomes "nearby numbers" (stored with **pgvector**).
* **OCR**: reading text from a picture of a page. **Tesseract** is the local OCR program. The **vision
  fallback** sends the page image to the generation model, with a transcription-only prompt, when
  Tesseract's result is not good enough.
* **RRF** (reciprocal rank fusion): combining two ranked lists by adding `1 / (60 + rank)` for each list a
  passage appears in.

### Files that matter, in reading order

1. `backend/migrations/versions/0009_ai_analysis.py`, `app/models/analysis.py` - the tables.
2. `app/sources/extract_child.py` - the sandboxed extractor (PDF text layer, Tesseract, figures, DOCX
   structure, Markdown/TXT). Standalone: imports only the standard library, pypdfium2 and Pillow.
   `app/sources/extract_sandbox.py` starts it and validates what it writes.
3. `app/ai/analysis/chunking.py` - policy `chunk-v1` (pure functions). `vision.py` - prompts, strict
   schemas, parsing of replies, the cross-check.
4. `app/ai/analysis/jobs.py` - enqueue, claim, lease, cancel. `gateway.py` - the one door to the
   providers for a job. `store.py` - building and publishing a generation.
5. `app/ai/analysis/pipeline.py` - the whole job. `app/workers/ai_worker.py` - the process around it.
6. `app/ai/retrieval.py` - hybrid retrieval. `app/services/analysis.py`, `app/api/analysis.py` - the API.
7. `app/commands/ai_retrieve.py`, `cleanup_ai.py`, `ai_status.py` - operator tools.

### The path of one analysis

```
POST .../versions/{v}/analyze  (owner/editor, CSRF)          ONE transaction, room row locked first
   role re-read under the lock -> AI gate (role, config, kill switch, capability, room switch,
   can_write on the anchor document, consent) -> keys decided (server or personal, per provider) ->
   page cap -> enqueue:  ready index with this configuration?  ->  "ready"  (no job, no provider call)
                         queued/running job exists?            ->  join it   ("joined")
                         otherwise                             ->  new job   ("created"; state `queued`)

AI worker (python -m app.workers.ai_worker), N claim loops
   claim_next  (UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED ORDER BY fairness, age))
   gate again  (same function, requester's own rights)   <- before ANY file or model access
   heartbeat task renews the lease every lease/3; cancel / lost claim / removal interrupt the stage
   1 extracting   sandboxed child reads the file (text layer, Tesseract on pages without one, figures)
   2 ocr          pages Tesseract could not read -> vision transcription (capped) -> else a GAP
   3 figures      each non-decorative figure: re-encode, store, describe with the vision model
   4 chunking     units -> passages (chunk-v1); figure passages appended
   5 embedding    batches: embed -> write chunks into a `building` generation (own short transaction each)
   6 publishing   validate, then ONE transaction: generation ready, version pointer switched, old
                  generation superseded, job succeeded
```

* **States users see** (`source_versions.analysis_state`): `not_analyzed`, `queued`, `extracting`
  (stages 1-4), `indexing` (5-6), `ready`, `failed`, `removed`. No percentages: the system does not know
  how far along a PDFium or Tesseract run is, and does not invent it.
* **Where transactions end.** Every database step is short. No transaction is open while the extractor
  child, Tesseract or any provider call runs. A ledger row, a budget reservation and a settle are each
  their own transaction (checkpoint 1).
* **Locks.** `enqueue` runs under the room lock and locks the version row. `claim_next` uses row locks
  with `SKIP LOCKED`. `publish` locks the job row and the version row, in that order, so removing a
  source (which locks the source and its versions) and publishing cannot interleave into a state where a
  removed source gets an index.
* **Fair claim order.** The job whose room has the fewest jobs running right now goes first, oldest first
  inside that, so one room queueing fifty files cannot starve the others (tested).
* **Cancellation.** `release_waiter` deletes the waiter and, if none is left, cancels a queued job at once
  or flags a running job (`cancel_requested_at`); the heartbeat notices within a third of the lease and
  interrupts the stage; the call policy also checks before every provider call and between retries.
  A late result is discarded; usage that already happened stays in the ledger.
* **Retry.** A crash (lease expiry) gives the job to another worker, up to `AI_ANALYSIS_MAX_ATTEMPTS`;
  then `sweep_abandoned` fails it (`attempts_exhausted`). A provider that stays down after the call
  policy's own retries requeues the whole job with a delay (30 s, 60 s, ... capped at 5 minutes). A
  worker that dies mid-build leaves a `building` generation; the next attempt deletes it first, so a
  crash and a retry yield exactly one generation (tested), and `cleanup_ai` deletes strays.
* **The first requester pays.** A shared job runs with the keys chosen for the first request, and its cost
  is charged to that person (or to the server budget if they use the server key). Later requesters get
  the index for free. Stated in the consent notice's spirit; not hidden.

### The extractor and local OCR

The extractor is a child process like the inspector (scrubbed environment, its own empty directory,
wall-clock deadline, POSIX rlimits, sockets refused) with two differences: it WRITES files into its
directory (`result.json`, page images for the vision fallback, figure candidates), and it may start
exactly one program, Tesseract, by the absolute path the parent resolved. The parent reads `result.json`
with a size cap and validates every field; anything malformed fails the extraction.

* **Per PDF page:** if the page has at least 10 non-space characters of text layer, that text is used
  (`text_layer`). Otherwise, if something is drawn on it, the page is rendered at `AI_OCR_DPI` (300) and
  Tesseract reads it. A page with nothing drawn is blank, not a gap.
* **The OCR quality gate** (a page FAILS if any is true): no words; fewer words than expected for the page
  size (`min(AI_OCR_MIN_WORDS_PER_PAGE, 0.25 words per square inch of page)`, at least 3); mean word
  confidence below `AI_OCR_MIN_CONFIDENCE` (60); more than `AI_OCR_MAX_IMPLAUSIBLE_SHARE` of the tokens
  look like keyboard mash; Tesseract crashed or timed out. **The last test is a heuristic, not a
  dictionary**: the standard library has no word list, so a token is "implausible" if it has no vowel
  (unless it is a short all-capital acronym), has six consonants in a row, repeats a character four
  times, or mixes letters and digits oddly. It catches symbol soup; it does not prove the words are
  right. Tesseract's own confidence is the stronger signal.
* **Failed pages** go to the vision fallback, at most `AI_VISION_FALLBACK_MAX_PAGES` (20) per source and
  within the budgets. The prompt is transcription only: exactly what is printed, `[illegible]` where it
  cannot be read, no summaries or corrections. **Cross-check:** the words Tesseract was sure about
  (confidence 80+, four or more letters) must also appear in the transcription; if fewer than half do,
  the passage is flagged `low_agreement` (shown as unverified) and a warning is recorded. With fewer than
  five sure words there is nothing to compare.
* **Gaps.** A page nobody could read (vision cap, vision unavailable, empty or mostly `[illegible]` reply,
  an unusable reply) is recorded as a gap with a reason. **More than `AI_UNREADABLE_FAIL_SHARE` (30 %)
  unreadable pages fails the version** (`too_many_unreadable_pages`): it is never marked ready.
  Otherwise it is ready with a visible `unreadable_pages` warning listing the pages.
* **Limits.** Pages per source (`SOURCE_MAX_PAGES`, checked at upload; `AI_MAX_NEW_ANALYSIS_PAGES`,
  checked at admission), OCR pages (`AI_OCR_MAX_PAGES_PER_SOURCE`), extracted characters
  (`AI_MAX_EXTRACTED_CHARS`), chunks (`AI_MAX_CHUNKS_PER_SOURCE`, a `chunks_capped` warning says how many
  were found and how many indexed).
* **DOCX** keeps heading levels (from `word/styles.xml`), list items, tables (rows, header first) and the
  order of the pictures. **Markdown** keeps `#` headings, lists and pipe tables. **TXT** is paragraphs.
  PDF headings come from the document outline when it has one.

### Figures

Candidates: embedded pictures (PDF and DOCX) and PDF pages that are mostly a drawing (little text, 20 or
more vector/image objects). **Decorative** ones are dropped before any model sees them: under
`AI_FIGURE_MIN_PIXELS` (10,000) or 32 pixels on a side, near-blank (one flat colour), or the same picture
on more than `AI_FIGURE_MAX_REPEATS` (3) pages (logos, letterheads). At most `AI_MAX_FIGURES_PER_SOURCE`
(40) are analyzed; if more exist the result says "N of M figures analyzed" (`figures_capped`).

Each kept figure is re-encoded through the existing image pipeline (`assets_pipeline.process_image`:
EXIF/ICC/text chunks gone, PNG/JPEG/WebP only, pixel limits), stored under an opaque key in
`STORAGE_PATH/figures/`, and described by the vision model with a fixed strict schema (kind,
description, visible text, elements, connections). The description becomes a **figure passage**
(`extraction = vision`) with the figure number and page in its locator. A figure whose reply is unusable
is skipped with a `figure_not_described` warning (its file is deleted); a provider outage is not a skip,
it fails or requeues the job.

**How figures are embedded** (`AI_FIGURE_EMBEDDING_MODE`): `description` (default: the description text),
`image` (the picture itself), or `image_plus_description` (one embedding of both, the model aggregates
interleaved parts). The default is `description` because it needs only the text-embedding capability and
is the only mode the documentation example does not leave open; **which mode retrieves best is an
empirical question for the evaluation harness (checkpoint 5), which needs real credentials.** If the
image-embedding self-check failed, `image` modes degrade to `description` with a warning, and the
generation records the mode actually used.

If the vision capability failed its self-check, no figure is analyzed and unreadable scanned pages are
gaps (`figures_not_analyzed` warning); text-layer pages and DOCX/Markdown/TXT still work.

### Chunking (`chunk-v1`)

Target about 400 tokens (`AI_CHUNK_TARGET_TOKENS`), hard maximum 900 (`AI_CHUNK_MAX_TOKENS`, validated
below the embedding model's input limit), 40 tokens of overlap inside a section. Token counts are the
conservative estimate of `pricing.py`, so real chunks are smaller than they look. A heading starts a new
chunk and is its first line; the heading path is in the locator; a page boundary closes a chunk once it
is half full; paragraphs are cut at sentences, and a single over-long sentence at word boundaries; tables
are one line per row with the header row repeated in every chunk the table needs and never share a chunk
with prose. Each chunk records `ordinal`, `text`, `text_hash` (SHA-256), `token_count`, `kind`
(`text`/`figure`), `extraction` (the least trustworthy way any of its text was obtained), the lowest OCR
confidence, `flags` (`low_agreement`), and a **locator**: `{heading_path, page_start, page_end}` plus
`figure_number` for figures. `section_key` (heading path, else a block of ten pages, else a block of
twenty chunks) is what checkpoint 4's summaries use to cover a file section by section.

### Embeddings and the index

* The text embedded for a passage is its heading path (if the text does not already start with it) plus
  the text; the **stored** text is exactly the passage. Documents use the document prefix, queries the
  query prefix (both settings, `docs/ai.md` section 7).
* Vectors live in `chunks.embedding` as pgvector's `vector` type **without a fixed dimension**, because
  the dimension is configuration. Integrity comes from three things: each generation records its model
  and dimension; a CHECK constraint makes every stored vector's `vector_dims` equal its row's
  `embedding_dims`; and publishing refuses a generation where any chunk is missing a vector or has
  another dimension. Retrieval's vector query only looks at generations whose recorded model and
  dimension equal the query vector's. **Vectors of different models or dimensions never meet** (tested).
* There is no ANN index. At the sizes of this phase (a few thousand passages in scope) an exact scan of
  the filtered rows is fast and always correct. Add HNSW only when measurements say so.
* The Compose database image is `pgvector/pgvector:pg16` (the official `postgres:16` plus the extension;
  the data volume stays compatible). Migration 0009 runs `CREATE EXTENSION IF NOT EXISTS vector`, which
  needs a role allowed to create extensions (the Compose user is a superuser; a managed database needs
  the extension enabled by its administrator). The downgrade leaves the extension installed on purpose.

### Publishing and what readers can see

Retrieval and the passages route follow only `source_versions.current_generation_id`. While a worker
builds, the new generation is `building` and the pointer still names the previous ready generation (or
nothing). `publish` re-checks that the job still owns the claim, is not cancelled and the source is not
removed, validates counts, ordinals, vectors and dimensions, then in one transaction marks the generation
ready, switches the pointer, marks the old generation `superseded`, and finishes the job. A reader sees
all of it or none of it. **Re-analysis never replaces a usable index with a partial one** (tested: a
re-analysis whose embedding fails leaves the old generation current). A superseded generation is kept
for `AI_SUPERSEDED_GENERATION_GRACE_HOURS` (24) so a request that pinned it keeps working; `cleanup_ai`
deletes it afterwards (checkpoint 4 adds "and nothing pins it").

### Retrieval

`retrieve(db, settings, room_id, generation_ids, query, query_vector)`:

1. **The scope is part of the query.** Every statement has `room_id = :room AND generation_id =
   ANY(:gens)` (and a join that excludes removed versions) in its WHERE clause, before ranking and
   limiting. A passage of an unselected, removed or other-room source cannot appear in any list. A static
   test pins the SQL text; behavioural tests prove it (five sources: select three, then the other two,
   and the second bundle shares no passage with the first; a query that only matches unselected sources
   returns nothing, with no fallback; a removed source is gone; another room's generation id yields
   nothing).
2. **Lexical:** the query's words, joined by `or` (`lexical_query`), through `websearch_to_tsquery
   ('english', ...)`, ranked with `ts_rank_cd` over the generated `tsv` column (GIN index), top 20 per
   generation. Only word characters survive, so the query cannot carry operators. Stop words are dropped
   by PostgreSQL. (Plain `websearch_to_tsquery` ANDs the words, so a natural question would almost never
   match one passage; a test found this.)
3. **Vector:** exact cosine distance to the query embedding, top 20 per generation, discarding hits farther
   than `AI_RETRIEVAL_MAX_DISTANCE` (0.6), so "nothing relevant" is a real answer.
4. **Fusion:** RRF with k = 60 over both lists, figure and text passages together. Ranks are computed per
   generation, so with several sources each contributes its own best passages.
5. **Packing:** fit into the evidence token budget; with several sources each keeps at least
   `AI_RETRIEVAL_MIN_SOURCE_SHARE` (20 %) of it from its own best passages before the rest is filled in
   fused order. The top `AI_RETRIEVAL_FIGURE_TOP_K` (5) figure passages are returned as the images to
   attach to the answer call (checkpoint 4 reads their bytes and enforces the image cap).

The result also says which generations were searched, which could only be searched lexically (another
embedding model or dimension) and which were unavailable (removed or gone), so a caller can fail or warn
instead of guessing.

**The inspect view.** `GET .../passages` shows the extracted passages of a version (owners and editors),
optionally narrowed by full-text search `?q=`. "Exactly which passages would this query retrieve" needs
the query embedded, and provider calls happen only in worker-class processes, so that view is an
operator command, not an API route: `python -m app.commands.ai_retrieve --room R --user U --version V
"question"` runs the same `retrieve` with the same scope and prints ids, locators, ranks and a short
excerpt (one real, ledgered embedding call with that user's key choice). In checkpoint 4 every request's
manifest records the retrieved passages, which is the permanent record.

### API reference

| Route | Who | Notes |
|---|---|---|
| `POST /rooms/{r}/sources/{s}/versions/{v}/analyze` | owner/editor with `can_write` on `document_id`, CSRF | body `{document_id}`; `202` `{status: ready/created/joined, job_id, generation_id, key sources, estimate}` |
| `DELETE .../analyze` | owner/editor, CSRF | withdraw YOUR request; cancels the job only if nobody else waits; `204` |
| `GET .../analysis` | any member | state, stage, job state and error code, waiters, counts, warnings, gaps. No text |
| `GET .../passages?after=&limit=&q=` | owner/editor (viewers `403`) | the current index's passages in order, with locators; `next_after` pages |

Gate order as in section 3, then: `404 source_not_found` / `source_version_not_found`,
`409 key_unavailable` / `key_rejected` (the provider key chosen for this person is missing or was
rejected; there is no silent switch), `409 analysis_cap_exceeded` (`cap`, `limit`, `actual`).
Job failure codes (on the version's `error_code` and the job): `too_many_unreadable_pages`,
`no_extractable_text`, `extracted_text_too_large`, `index_incomplete`, `analysis_cap_exceeded`,
`budget_exhausted`, `key_unavailable`, `key_rejected`, `attempts_exhausted`, `extraction_timeout`,
`extraction_failed`, the file codes of section 9, the gate's own codes (`insufficient_role`,
`room_access_lost`, `ai_disabled_for_room`, `ai_disabled`, `ai_write_access_required`,
`consent_required`, ...) and the provider codes (`provider_unavailable`, `provider_bad_request`, ...).

### Removal, cleanup and operator commands

* **Removing a source** now also, in the same transaction: cancels its queued and running jobs (a running
  worker finds its claim gone and stops), clears the version pointers and deletes every generation of its
  versions, which cascades to chunks, vectors and figure rows. The figure image files are deleted right
  after the commit (a failure leaves an orphan that cleanup removes). Redacting stored excerpts and
  manifests comes with checkpoint 4. Published chat text stays (section 10, limits).
* **`cleanup_ai`** adds: stale `building` generations (no live job owns them), `failed` generations,
  `superseded` generations older than the grace period, and orphan figure files older than 24 hours.
* **`ai_status`** adds the analysis queue (jobs queued/running, age of the oldest) and job failures by
  code over 24 hours. **`ai_retrieve`** is the inspect view above.
* **`python -m app.workers.ai_worker`** runs the self-check loop and `AI_WORKER_CONCURRENCY` claim loops;
  `--drain` runs queued jobs and exits; `--once` is the self-check only.
* **Docker:** `backend/Dockerfile` (Python 3.12 + Tesseract with English data) and the `ai-worker` Compose
  service (profile `ai`) that mounts the same storage directory as the API.

### Decisions and deviations, stated plainly

* **Google Drive** is still deferred by the owner; the sources and analysis tables do not depend on it.
* **`source_updated` and `ai_request_updated` events** are checkpoint 4 (event type, schema, replay and the
  Part 5 client change together). Until then clients poll `GET .../analysis`.
* **"Analyze now" is allowed to owners and editors only, with a document**, because every AI action has an
  anchor document for the permission check (the brief's anchor rule). The worker re-runs the same gate
  with the requester's rights when it claims the job; a change of role after that does not tear down a
  job in flight (keys were decided at admission), but the person's next job is refused (tested).
* **The page cap** `AI_MAX_NEW_ANALYSIS_PAGES` (default 100) is checked per file at admission. A 150-page
  PDF is refused with `analysis_cap_exceeded` until the setting is raised: a deliberate default, since
  analysis spends money.
* **Per-request budget caps for analysis** use the job's own snapshot (`caps`): the provider-call cap is
  `AI_ANALYSIS_MAX_PROVIDER_CALLS`, the image cap derives from the vision and figure caps, and the
  per-call output cap is the smaller of the vision output size and `AI_MAX_OUTPUT_TOKENS`.
* **Bug found by the tests and fixed:** Google reports an invalid key as HTTP 400 `API_KEY_INVALID`, and
  only the key-validation call translated that; a rejected key during a job surfaced as a generic bad
  request. It is now `key_rejected` everywhere.

## 11. Checkpoint 4: `@AI` requests and cited answers

A person with write access to a document chooses `@AI` in the chat composer, picks files (and optionally
the open document, selected blocks, or some chat messages) and asks. The system waits for the files to
be analyzed, gathers evidence from exactly those files and exactly that context, asks the generation
model for a structured answer, **validates it on the server**, and posts one reply in the chat. Nothing is
ever written into a document (that is Phase 2).

Terms used below:

* **Request** (`ai_requests`): one `@AI` question with its inputs, state and keys. It is created together
  with its chat message in one transaction.
* **Pin**: at admission the request records the exact source *versions* it uses
  (`ai_request_sources`). When they are ready, it also freezes each version's index *generation*. A newer
  version of a file, or a re-index, never changes what a running request reads.
* **Label**: the model never sees database ids. Evidence is labelled `P1..Pn` (retrieved passages),
  `S1..Sk` (section summaries), `D1..Dm` (blocks of the open document) and `C1..Cj` (chat messages).
  Labels are local to one request. The server maps a cited label back to what it stands for; a label
  that does not exist is invalid.
* **Manifest** (`ai_manifests`): the record of what was actually sent for one attempt, written before
  the answer is generated and never changed (except by redaction, below).
* **Coverage statement**: computed by the server from counts, never written by the model: per file the
  sections read of the total and the mode (`full`, `sampled`, `retrieval`), figures examined versus
  found, the document's blocks included versus total with the update counter it was captured at.

### Files that matter, in reading order

1. `backend/migrations/versions/0010_ai_requests.py`, `app/models/ai_request.py` - tables; `messages.kind`.
2. `app/services/ai_requests.py` - **admission**, the state machine, cancel/retry, claiming, publication.
3. `app/api/ai_requests.py` - the routes.
4. `app/ai/answer/execute.py` - one request in the worker. `strategies.py` - evidence per task kind.
   `capture.py` - the open document. `prompts.py` - prompts and how untrusted content is delimited.
5. `app/ai/answer/answer.py` - the answer schema and **all server-side validation**.
   `mermaid.py` + `packages/editor-schema/src/mermaid.ts` + `packages/mermaid-check` - diagrams.
6. Frontend: `frontend/src/ai/` (`store.ts`, `Composer.tsx`, `Cards.tsx`, `SafeMermaid.tsx`, `Panels.tsx`),
   `client/src/room-client.ts` (the AI signals).

### The path of one request

```
POST /rooms/{r}/ai-requests  (owner/editor, CSRF)                  ONE transaction, room row locked first
  role re-read under the lock; same client_request_id? -> the same request (200) or 409 if inputs differ
  -> AI gate (role, config, kill switch, capability, room switch, can_write on the anchor document, consent)
  -> keys decided per provider (server or personal; recorded; never swapped later)
  -> capacity (per user 3, per room 10 active) -> pinned versions resolved (this room, not removed)
  -> chat ids validated -> estimate + caps (new-analysis pages/figures) -> confirm_large if over threshold
  -> the human write bucket is consumed -> INSERT message(kind=ai_request) + request + pins
  -> analysis jobs enqueued for files not ready (the request is a waiter) -> ai_request_updated event
  state: queued (all files ready, generations frozen)  |  waiting_for_sources

AI worker loop (advance, expire, sweep, claim)
  waiting_for_sources -> queued when every pinned version is ready (generations frozen);
                         -> failed source_removed / source_failed (never skipped, never replaced by a newer one)
  claim: UPDATE ... FOR UPDATE SKIP LOCKED, fair across rooms, lease + token
  0 gate again (requester's rights)  1 classify (auto only)  2 capture the document  3 gather evidence
  4 manifest  5 answer call  6 validate / repair  7 re-gate, re-check files, publish in ONE transaction
```

* **States and events.** `waiting_for_sources`, `queued`, `running`, `validating`, `succeeded`, `failed`,
  `cancelling`, `cancelled`, `expired`. Every transition records `ai_request_updated {request_id, state}`
  in the same transaction as the change. A request waiting or queued longer than `AI_REQUEST_TTL_MINUTES`
  (30) expires and releases its analysis jobs.
* **Lock order** is always the room row first, then the request row. The worker's claim is the one
  exception (it takes only the request row, because it does not know the room yet and records no event);
  "running" is announced by a second step. Taking them in the other order anywhere would deadlock.
* **Exactly one reply.** `ai_results` is `UNIQUE (request_id)` and the reply message has a client message
  id derived from the request id, authored by the system user, which the messages table makes unique. A
  worker that crashes and is replaced may repeat the computation (and spend); it cannot publish twice
  (tested by running a second worker over an already-published request).
* **Late results.** Cancellation is checked at every stage and before every provider call. A result that
  arrives after a cancel, a lost claim or a removed source is discarded; its usage stays in the ledger.
* **Retries.** The requester may retry a failed, cancelled or expired request up to
  `AI_REQUEST_MAX_ATTEMPTS` (3) attempts in total; it re-checks everything, uses the same pinned versions
  and the same recorded key sources, and starts a new manifest attempt. A provider outage requeues the
  request with a delay; a worker that keeps dying is failed after 5 claims.
* **Chat messages.** An `@AI` message is an ordinary message with `kind = ai_request` (so it has its
  sequence, event, outbox row and counts against the human write bucket). It can never be edited
  (`409 message_locked`) and an edit never reruns AI. Deleting it cancels an unfinished request. The reply is
  `kind = ai_answer`, authored by the **system user** "ThreadForge AI" (an active `users` row with no
  authentication identity, no sessions and no membership, so it appears in no member list, presence or
  invitation). It cannot be edited; the owner can delete it, which redacts the stored result. The schema
  impact is two columns on `messages`, immutable after insert (a trigger enforces it), and two event
  types; nothing else changed.

### Who may do what

| Action | Owner | Editor | Viewer |
|---|---|---|---|
| Send / retry / cancel own request | with `can_write` on the anchor document | same | `403 insufficient_role` |
| Cancel any request in the room | yes | no | no |
| Request progress, context summary | requester; others only with `can_write` on the request's document | same | `403` (they see the chat message only) |
| Read the published answer | yes | yes | yes |
| Citation text and figure thumbnails | roles in `SOURCE_DOWNLOAD_ROLES` | same | same |
| Delete an AI answer | yes | no (`403`) | no |

Outsiders and removed members get `404` everywhere. Gate codes as in section 3.

### The request, field by field

`client_request_id` (also the message's id), `instruction` (1-4,000 characters; control characters
removed), `task_hint` (`auto`, `ask`, `summarize`, `compare`), `document_id` (the **anchor** for the
permission check; the document's content is only used when asked for), `include_document_content` with
`document_scope` (`none` | `whole` | `blocks`), `selected_block_ids` + `selection_hash`,
`source_version_ids` (at most 10), `include_chat_context` with explicit `chat_message_ids` (at most 20),
`confirm_large`. Unknown fields are refused: a client cannot supply an actor, a role, a key source or a
price. A request needs a file or the document (`422 ai_context_required`): chat messages are context,
never evidence, so they cannot be its only material. A chat message that is deleted, from another room or
an AI answer is refused (`422 invalid_ai_request`, reason `chat_messages`). In "sources only" mode, no
document text, no chat and no earlier AI output ever reaches the model (tested on the actual prompt).

### Task kinds

| Kind | How the evidence is gathered |
|---|---|
| `qa` | embed the instruction, hybrid retrieval over the pinned generations (section 10), one answer call |
| `compare` | the same retrieval **per source** with an equal share of the evidence budget, so one long file cannot crowd out another; one comparison call |
| `summarize` | not retrieval: each file is cut into sections (heading path, else page ranges), sections are summarized in batches (map calls, capped), the answer call combines the summaries (reduce). Above `AI_SUMMARIZE_SOURCE_TOKENS` the sections are **sampled evenly, first and last always kept**, and the coverage statement says `sampled` |
| `document_review` | the captured document as labelled blocks; too long for the budget -> sections by heading, map then reduce |

`auto` runs one small classification call with **only the instruction text** (tested: no evidence reaches
it). It can pick among strategies the request's own context supports; it can never widen scope: `compare`
with fewer than two files becomes `qa`, `summarize` with no files becomes `document_review` (if the
document is included) or `qa`, `document_review` without the document becomes `qa`. A failed
classification means `qa`. If retrieval finds nothing relevant and there is no document or chat context,
the server answers "I could not find anything relevant" **without calling the model** (it could only
invent something).

### Reading the open document (read-only)

When the document is included, the worker asks the collaboration service's private route
`POST /internal/ai/read` for the requester. The service stores pending edits, validates the content with
the shared schema and answers with the content, its hash, the exact `yjs_update_counter` and every
block with an id (type, text, own hash). It re-runs the same `can_write` as the editor, so a demoted user
gets `403` there too. The capture is taken **once per attempt and bound to `document_id`**: a later change
of the room's active document never redirects a running request (tested). The deprecated JSON column and
stale snapshots are never used; if no reliable capture is possible the request fails
(`capture_unavailable`, retried later; `document_unavailable`; `ai_write_access_required`).

* **What the server cannot see.** Edits that have not yet reached the collaboration service. The composer
  disables "Use open document" and "Use selected blocks" while the editor reports unsynced changes, and the
  answer card states the update counter it was captured at.
* **Selected blocks.** The client sends the ids of the top-level blocks it highlighted and
  `selection_hash = sha256("\n".join(f"{id}:{block_hash}"))` over them in document order, where
  `block_hash` is the shared package's `blockHash` (the function the service uses). If a block is gone or the
  hash differs, the text changed after it was selected: `selection_changed`, "select it again".
* **Document images** inside the scope are attached under the same image cap, read from asset storage
  with the room checked.

### Prompting and untrusted content

Sources, document text, chat, OCR text, figure descriptions, file names and earlier model output are
**data**. They reach the model only between `===== BEGIN UNTRUSTED DATA <random> =====` and the matching
END line, where the token is generated per call (a file cannot contain the closing line in advance). The
system prompt (`answer-v1`, recorded in every manifest) says that the data cannot change its rules, the task,
who may see what, or what the model may do; that instructions inside it are content to describe; and that
it has no tools or network. The request body contains no `tools` field (tested). The honest limit: a
model can still be *influenced* by hostile text and write a wrong answer; what the design guarantees is
that it cannot *act*, and that the server's checks below bound how wrong a published answer can be.

### The answer and its validation

The model returns blocks of type `paragraph`, `bullet_list` or `diagram`, each claim with citations
`{ref: "P3", quote: null | "exact words"}`, plus `evidence_gaps`. The server (`answer.py`):

* refuses a reply of the wrong shape (once repaired, else `answer_invalid`);
* removes citations that name no existing label (chat labels are not citable);
* keeps a **quote** only if it is a whitespace-normalized substring of the cited passage **and** the
  passage came from a text layer, Word text or plain text. On OCR, vision, figure and section evidence
  a quote is dropped and the citation is marked *machine-generated*; a quote that is **not** in the passage
  removes that citation (a fabricated quote makes it untrustworthy);
* flags a claim with no valid citation as `unsupported` (shown as "(no citation)" in the card and the
  chat text);
* if **more than 30 %** of the claims lose support (or none keeps any), one repair attempt with the reason;
  still bad: the request fails `answer_unsupported` and publishes nothing.

A citation shows which passage the answer *referenced*. It does not prove the passage supports the claim;
the card says so. A published result keeps only excerpts of at most 300 characters.

### Diagrams (Mermaid)

* **Static rules** (shared by the server, the checker and the browser from one set of test cases): only
  `flowchart`, `graph`, `erDiagram`, `sequenceDiagram`, `classDiagram`, `stateDiagram(-v2)`, `gantt`,
  `mindmap`, `timeline`; at most 20,000 characters and 500 lines; no `%%{ }%%` directives (they can change
  Mermaid's security level), no `click`/`href`/`link`, no HTML tags, no URLs, no `javascript:`/`data:`, no
  entities. They hold even when Node is missing.
* **Where the parse check runs - decision:** **server-side, in a sandboxed Node child**
  (`packages/mermaid-check`: Mermaid under jsdom, scrubbed environment, wall-clock deadline). Reason: the
  answer path wants to repair a bad diagram once with the parser's own message, and to know before
  publishing. Measured here: about 3.4 s cold start for a batch, about 20 ms per diagram afterwards. The
  browser parses again when it draws. If Node is not available the diagram is published `unchecked`
  (never silently `ok`). A diagram that still fails after its one repair is published with its **source and
  the error**, never an empty box.
* **Drawing** (`SafeMermaid`, shared by chat cards and reused in Phase 2): Mermaid 12.2.0 (pinned),
  `securityLevel: "strict"`, `htmlLabels: false`, a fixed theme, bounded text/edges, an 8 s render timeout, a
  size limit, and DOMPurify (3.4.16, SVG profile; script, foreignObject, iframe, use, image, links and all
  event attributes removed) on the result. Hostile text and hostile SVG are tested.

### Events and the client

`ai_request_updated {request_id, state}`, `source_updated {source_id, source_version_id, state}` (a new
version, an analysis that became `ready`/`failed`, a removal) and `room_ai_settings_changed` carry **ids and
states only** (the payload schemas reject anything else; a test checks no instruction text appears). The
room client folds them into `AiSignals` and delivers them to the application **before the cursor advances**
(same contract as document signals: if the handler throws, the batch is retried and replayed), plus
`reset` after a fresh bootstrap. Messages carry `kind` and `ai_request_id` in every read path (history, sync,
single). Stage labels ("Waiting for files", "Analyzing files", "Gathering context", "Generating",
"Validating") come from polling `GET .../ai-requests/{id}` with growing delays while a card is on screen;
there are no per-token events.

### API reference

| Route | Who | Notes |
|---|---|---|
| `POST /rooms/{r}/ai-requests` | owner/editor, CSRF | `201`, or `200` for a repeated `client_request_id`; `409 confirmation_required` (with the estimate) until `confirm_large` |
| `POST /rooms/{r}/ai-requests/estimate` | member, CSRF | now accepts `source_version_ids`; includes the cost of analyzing files not analyzed yet |
| `GET .../ai-requests/{id}` | requester, or owner/editor with `can_write` | state, stage label, queue position, sources, `can_cancel`, `can_retry` |
| `GET .../ai-requests/{id}/context` | same | the authorized context summary: files and versions, counts, document update counter, models; never the text |
| `POST .../{id}/cancel`, `POST .../{id}/retry` | requester (cancel: or owner) | |
| `GET .../{id}/result` | any member | the answer, citations (excerpts only for `SOURCE_DOWNLOAD_ROLES`), coverage |
| `GET .../{id}/citations/{n}`, `.../image` | `SOURCE_DOWNLOAD_ROLES` | the resolved passage; the stored figure image (`no-store`, `nosniff`, sandbox CSP) |

New codes: `ai_request_not_found`, `ai_request_id_reused`, `ai_context_required`, `invalid_ai_request`
(+ `reason`), `ai_capacity` (429, `scope`), `confirmation_required`, `ai_request_not_cancellable`,
`ai_request_not_retryable` (+ `reason`), `message_locked`, `citation_not_found`. Request failure codes
(`error_code`): `source_failed`, `source_removed`, `selection_changed`, `capture_unavailable`,
`document_unavailable`, `answer_unsupported`, `answer_invalid`, `budget_exhausted`, `request_expired`,
`execution_attempts_exhausted`, plus the gate's and the provider's codes.

### Removal, retention and cleanup

* **Removing a source** now also redacts: in published results the citations to its versions lose
  `excerpt`/`quotes` and become stubs ("source removed", with version id and locator); in manifests the
  stored passages of those versions lose their text. **Already-published chat text stays** (stated limit).
  The figure thumbnail route returns `404`.
* **Deleting an AI answer** (owner) wipes the stored answer and the citation excerpts and quotes, keeps the
  citation stubs, and redacts the manifests of that request.
* **Retention.** `retain_until = finished_at + AI_RESULT_RETENTION_DAYS` (30). `cleanup_ai` deletes
  manifests past it (passages, document captures, chat copies, records of images sent) and keeps the
  published answer with its short excerpts. It also deletes expired budget windows and rate buckets and
  never deletes a superseded index that an unfinished request pins.

### The frontend

* **Composer.** `@AI` is an entity chosen from a menu (typing `@` offers it); typed `@AI` text is an ordinary
  message. Removable chips show the chosen files (each with its state, and "will be analyzed when you send"
  with a size for unanalyzed ones), "Use open document" (with the real title, disabled while unsynced),
  "Use selected blocks", "Include chat context" (off by default; messages are ticked in the chat list),
  and task chips Auto / Ask / Summarize / Compare. Output is "Answer in chat" only. The estimate and the
  key that would be charged show before sending; a large request asks to confirm. Selections reset after
  each send. For viewers the AI controls are replaced by a short explanation.
* **Cards.** The requester sees a request card (state, stage label, queue position, a plain reason on
  failure, Cancel / Try again). Every member sees the answer card: text only (never HTML or links),
  numbered citations that open the passage, locator and a figure thumbnail, "(no citation)" on unsupported
  claims, a machine-generated note on OCR/vision evidence, evidence gaps, the coverage statement and the
  "citations are references, not proof" note. Diagrams go through `SafeMermaid`.
* **Files panel and AI settings.** List, status, upload, replace, remove, download, inspect extracted
  passages, "Analyze now"; the data notice, personal keys, the owner's switch and usage. Chat and
  everything else stay usable when AI is off, unavailable, out of budget or has no key.

### Decisions and what this checkpoint does not do

* **Google Drive** remains deferred by the owner (no Drive connection exists in the repository).
* **Provider rate-limit backoff** is the call policy's (checkpoint 1) plus a requeue with a delay; the
  **queue position** shown is the request's rank among queued requests by age.
* **The classifier and the map step use the generation model**, as the brief's single generation model.
* **An AI answer's reply text** (the chat message) is a plain-text rendering of at most 4,000 characters with
  `[n]` citation marks and a pointer to the card; the card is rendered from the stored result, so what
  viewers see is the validated structure, not the text blob.
* **Not verified against the real providers:** whether the strict structured-output mode accepts the
  `["string", "null"]` unions in the answer schema (the checkpoint-1 self-check uses a simpler schema);
  see section 8.

## 12. Limits (stated plainly)

* If prices are misconfigured too low, spend control is lost: budgets count what the configuration says.
* Embedding usage is estimated (Google reports none), so embedding spend is approximate.
* A worker crash between a provider call and its ledger row can lose that row (the spend still happened).
* A file removed from a room stays in already-published chat answers (their text is never rewritten).
* Superseded source versions stay stored (and count toward the quota) until the source is removed;
  checkpoint 4 decides which can be dropped once requests pin versions.
* Inspection cannot tell a scan from a drawing; an unreadable scan is discovered when OCR runs at first
  use (checkpoint 3 does that: such pages become gaps, and over 30 % unreadable fails the version).
* OCR and figure descriptions can be wrong. Tesseract's confidence and the plausibility heuristic are
  signals, not proof, and a vision transcription is a model's reading of a picture.
* Caps can leave parts of a large file unanalyzed (chunks, figures, OCR and vision pages). The result of
  every analysis states what was left out (`warnings`, `gaps`); a caller must show it.
* A shared analysis job is paid for by whoever's request created it.
* A citation is a reference to the passage the answer used, not proof that the passage supports the claim.
  The server checks that the cited passage exists, that a quoted string really occurs in it, and that no
  more than 30 % of the claims lack support; it cannot check that the passage means what the answer says.
* The answer only sees the document as last synced to the collaboration service. Edits still in a browser
  tab (not yet synced) are invisible to it. The composer disables "Use open document" while the editor
  reports unsynced changes in *your* tab, and the request card says unsynced edits in anyone's browser
  are not visible; changes unsynced in someone else's tab cannot be detected.
* Mermaid diagrams are shown in the chat card only. They cannot be exported (no PNG/SVG download, no
  insertion into a document); a diagram block is validated and sanitized, not guaranteed to be pretty.
* Retrieval ranks the right page first about two times in three on look-alike pages in the stub run
  (section 13); real-embedding behaviour is unmeasured.
* Costs and tokens in the evaluation are placeholders until the real run is done.

## 13. Checkpoint 5: evaluation

The harness lives in `backend/tests/eval/` and is **not part of CI** (it only runs with `RUN_EVAL=1`; the
normal suite skips it). It sends a gold set of questions through the real upload, analysis, retrieval,
request and answer code, and writes a report to `backend/tests/eval/results/`.

```
cd backend
RUN_EVAL=1 uv run pytest tests/eval -s                              # local provider stand-ins (default)
RUN_EVAL=1 EVAL_PROVIDER=real uv run pytest tests/eval -s           # the real providers (needs keys in the env)
```

### What it contains

* `corpus.py`: four small synthetic sources, generated in code (no real content):
  `depots.pdf` (a text PDF, eight pages, one depot each, with look-alike filler so a question has to find
  the right page), `march-scan.pdf` (three scanned pages: a picture of printed text with no text layer,
  read by Tesseract), `handbook.docx` (headings, a table and an embedded architecture diagram) and
  `release.md` (headings, a list, a table). A standalone image is **not** an accepted source type in
  phase 1 (pdf, docx, txt, md), so the "diagram image" is the picture inside the Word file.
* The gold set (13 questions): 7 factual (one across two sources), 1 figure question, 1 summary, 1
  comparison, 1 document review (with a fake collaboration service holding a small document) and 2
  "draw a diagram" requests. Each names the phrases the retrieved evidence must contain ("needles"), the
  sources the answer must cite, and whether a figure citation or a rendered diagram is expected.
* `metrics.py` (unit-tested in `tests/test_eval_metrics.py`, which is part of the normal suite): recall at
  k, character accuracy (edit distance on normalized text), percentiles (nearest rank), shares.
* `test_eval.py`: the runner and the report writer.

### The two modes, and what each number means

| Mode | Providers | What the numbers mean |
|---|---|---|
| `stub` (default; what has been run) | the HTTP stand-ins the tests use | Real: files, sandboxed extraction, **real Tesseract**, chunking, PostgreSQL full-text search and pgvector, ranking, validation, publication, server-computed coverage. **Not real**: anything a model decides (citations, diagrams, schema compliance), token counts, cost, network and model latency, and the vector half of retrieval (a bag-of-topics stand-in for embeddings). The report labels every row. |
| `real` | the configured providers | Every row measures those models. **This mode has never been run** (no credentials were available); it is written to use the same code path without the stub and is therefore unverified until the owner runs it. |

### Results of the stub run (2026-11-09)

13 of 13 gold items published an answer. `backend/tests/eval/results/stub-results.md` holds the full
report; the rows that mean something:

| Metric | Value | Basis |
|---|---|---|
| Requests that published an answer | 100% (13/13) | measured |
| Retrieval recall@1 / @3 / @5 (mean share of required phrases found) | 65% / 95% / 95% | measured: real chunking, SQL full-text and ranking; vector part is a stand-in |
| Required phrases found anywhere in the evidence sent to the model | 100% | same |
| OCR character accuracy, three clean synthetic scans | 92% | measured, real Tesseract |
| Summaries/comparisons that report full section coverage | 100% | measured (computed by the server) |
| Citation validity, schema-valid without repair, diagrams that render, figure citations | 92-100% | **stub: not a model measurement** |
| Tokens, cost per task type | placeholders | **stub: not measured** |
| Latency p50 / p95 | 0.75 s / 3.3 s | pipeline time only; the 3 s is the Node Mermaid check |

### What the stub run does tell us

* **Retrieval ranks the right page first only about two times in three on look-alike pages.** The
  full-text half uses `ts_rank_cd`, which does not weigh how rare a word is, so "Berlin" counts no more
  than "parcels" when every depot page mentions parcels. The correct passage is in the first three for 95%
  of the phrases and always in the evidence, so answers are not starved, but a model sees the pages in a
  less helpful order. Whether the real embeddings repair the ranking is exactly what the real run must
  show; if not, candidates are a rarity-aware lexical score or a reranking pass. Not changed in this
  phase: the brief fixes `ts_rank_cd`, and the numbers that would justify a change do not exist yet.
* **Tesseract reads clean synthetic scans at 92%, not 100%.** It is one real number for the ceiling on
  clean input; real scans are worse. Pages it cannot read go to the vision fallback (section 10).
* The pipeline itself holds up on mixed sources, a figure, a document review and diagrams; nothing failed
  to publish, and every request used 2-3 provider calls.

### What it does not tell us

Anything about answer quality of the real models, real cost, real latency, real embedding ranking, real
OCR on messy scans, or bigger sources (the corpus is tiny: differences of a few points are noise). The
owner is meant to judge quality from a `real` run; **Phase 2 should not rest on the stub numbers.**

## 14. Checkpoint 5: security and regression sweep

The checklist of the brief and where each item is proven:

| Item | Where |
|---|---|
| Keys encrypted, never logged or returned | `test_ai_api.py` (status reveals no secrets; a saved key is validated, encrypted and never returned), `test_ai_core.py` (AES-GCM bound to its context), `test_ai_answers.py` (no content or key in logs) |
| No content, file names or prompts in logs | `test_analysis_lifecycle.py::test_logs_never_contain_file_names_or_content`, `test_ai_answers.py::test_every_request_is_recorded_in_the_ledger_with_its_id_and_no_content_is_logged` |
| Permission table on every route | `test_ai_requests_rules.py`, `test_sources_api.py`, `test_ai_api.py`; **new** `test_ai_security_sweep.py`: the AI HTTP surface equals a reviewed list of 31 method+path pairs (a new route fails until reviewed), every route refuses an anonymous caller, and an outsider gets the *same* answer for a real room and a missing one on every room route |
| Outsiders change nothing | **new** `test_ai_security_sweep.py::test_an_outsiders_attempts_change_nothing` (row counts unchanged after every write route is tried) |
| Internal routes private | **new**: no `internal` path in the public API's OpenAPI schema, and `/internal/ai/read` is not served by it (it belongs to the collaboration service, which checks the shared secret) |
| Provider calls only in workers | **new**: an import scan pins who may build provider adapters (the gateway, the key check, the self-check), who may use the metered gateway (the two workers' modules and one operator command) and that no API route module, service or model imports either |
| All retrieval filters inside the SQL | **new**: every retrieval statement is checked for `room_id` and the pinned generation ids inside the query text; behaviour is in `test_retrieval.py` (five sources, select three then two, exact passages, no fallback, a deleted source never retrievable) |
| Prompt-injection containment | `test_ai_answers.py::test_instructions_inside_sources_are_data_inside_a_delimited_block`, `test_analysis_lifecycle.py::test_instructions_inside_a_file_are_indexed_as_text_and_trigger_nothing`, `test_analysis_vision.py`, `test_answer_validation.py` (model output is validated; links pass the scheme policy; nothing named by model output is fetched) |
| Quotas and caps cannot be bypassed by retries or parallel requests | `test_ai_requests_rules.py` (parallel duplicates make one request, caps per user and room, a retry re-checks everything), `test_ai_core.py` (concurrent reservations never overshoot the budget), `test_sources_api.py` (quota counts uploads in flight; parallel intents cannot both take the last slot) |
| Removed sources unreachable | **new**: every read route of a removed source answers 404/409/410; behaviour of redaction in `test_ai_requests_more.py` |
| Regressions of chat, events, documents, collaboration | the whole backend suite (1983 passed before checkpoint 5), the room-event audit tests, `client/` 76, collaboration 64, editor-schema 126, frontend 84 |

Findings of the sweep: none that needed a code change. The sweep is a guard, not a penetration test:
nobody attacked the running system from outside, and the hostile-file and PDFium caveats of section 8
still stand.

## 15. Final report (phase 1)

### What phase 1 delivers

Bring-your-own-key or server keys with spend controls and a consent notice (checkpoint 1); device
uploads inspected in a sandbox (2); lazy analysis with local OCR, a vision fallback, figures, chunking,
embeddings and hybrid retrieval (3); `@AI` requests with pinned sources, validated cited answers, Mermaid
diagrams, document review and the chat UI (4); the evaluation harness and a security sweep (5). Nothing
writes into a document: that is phase 2, not started.

### Suggested reading order

1. This file, sections 2 to 4 (spend, keys, permissions), then `docs/ai-progress.md` (what was verified).
2. `backend/app/ai/calls.py` (every provider call goes through `MeteredCaller`), `ai/keys.py`.
3. `backend/app/services/sources.py`, `app/sources/` (uploads and the sandbox).
4. `backend/app/ai/analysis/pipeline.py`, `jobs.py`, `store.py` (analysis and the atomic publish), then
   `app/ai/retrieval.py`.
5. `backend/app/services/ai_requests.py` (the request state machine), `app/ai/answer/execute.py`,
   `strategies.py`, `answer.py` (validation), `mermaid.py`.
6. `frontend/src/ai/` (`store.ts`, `Cards.tsx`, `Composer.tsx`, `Panels.tsx`, `SafeMermaid.tsx`).
7. `backend/tests/eval/` and `tests/test_ai_security_sweep.py`.

### API reference

Per-checkpoint tables: sections 4, 9, 10 and 11. The complete list is pinned by
`test_ai_security_sweep.py` (31 method+path pairs). Error codes added by phases 1-4, all with the
`{"error": {"code", "message", "request_id"}}` envelope:

| Status | Codes |
|---|---|
| 503 | `ai_not_configured`, `ai_disabled`, `ai_unavailable` (with the failing capability) |
| 403 | `consent_required`, `ai_write_access_required` |
| 409 | `ai_disabled_for_room`, `ai_anchor_required`, `key_unavailable`, `source_upload_not_open`, `source_upload_id_reused`, `source_unavailable`, `analysis_cap_exceeded`, `ai_request_id_reused`, `ai_request_not_cancellable`, `ai_request_not_retryable` |
| 404 | `source_not_found`, `source_upload_not_found`, `source_version_not_found`, `ai_request_not_found` |
| 422 | `key_rejected`, `invalid_source`, `ai_context_required`, `invalid_ai_request` |
| 429 | `ai_capacity` (plus the budget and rate codes of section 4) |

### Settings

`.env.example`, section "ThreadForge AI" (about 80 settings: `AI_*` and `SOURCE_*`), each validated at
startup with the setting named in the error. New with checkpoints 3 and 4: the analysis and OCR
settings, `AI_RETRIEVAL_*`, `AI_REQUEST_*`, `AI_NODE_COMMAND`, `AI_MERMAID_CHECK_SCRIPT`,
`COLLAB_SERVICE_URL`.

### The privacy notice (served by `GET /ai/consent`, version 1)

* When a file you attach is analyzed for the first time, its text and images are sent to the embedding
  provider so they can be searched.
* To answer, the passages retrieved from your files, any images selected for the answer, the document
  text you include, and any chat messages you include are sent to the generation provider.
* Scanned pages that local text recognition cannot read are sent to the generation provider's vision
  model to be transcribed.
* Requests use either your own API key or the server's key. You will be told which one before you send.
* Other members of the room can already read the files you attach; AI answers are visible to everyone in
  the room.
* Answers can be wrong. A citation shows which passage the answer used, not that the passage proves the
  claim.

### Values to confirm

Section 7 (models, windows, prices, dimensions; all read from the provider documentation on 2026-11-08).
Added since: `AI_RETRIEVAL_MAX_DISTANCE` (the cosine-distance cutoff) and `AI_RETRIEVAL_RRF_K` (60) are
starting values never tuned on real embeddings; the evaluation is where to tune them. The per-request
caps (`AI_MAX_PROVIDER_CALLS`, `AI_MAX_NEW_ANALYSIS_PAGES`, `AI_REQUEST_MAX_CHAT_CONTEXT`) are
defaults, not measurements.

### Unverified assumptions (everything below ran only against stand-ins)

1. All items of section 8, unchanged: no real provider call has ever been made by this code.
2. Whether strict structured output accepts the answer schema's `["string", "null"]` unions and the
   diagram block shape (section 11).
3. That `gemini-embedding-2` ranks a figure's description or image next to a question about it, and
   which `AI_FIGURE_EMBEDDING_MODE` is best: the brief says to evaluate this on the gold set, and that
   needs the real run.
4. The vision model's transcriptions and figure descriptions: the only checks are the plausibility
   heuristic and the cross-check against Tesseract on the same page.
5. Linux behaviour of the extractor child's resource limits (`extract_child.py`) and of the POSIX sandbox
   tests: the automated runs were on Windows, where those tests skip. The Docker image
   (`backend/Dockerfile`, with Node and Tesseract) was written but **not built** in this environment.
6. Only Chromium ran the browser spec; Firefox and Safari did not.
7. The `real` evaluation mode (section 13) has never run.
8. Scale: the largest source tested is a few dozen pages; the exact (non-ANN) vector search is expected
   to be fine at room scale but was not measured at thousands of chunks.

### Known limits

See section 12 (the list is complete as of phase 1). In short: wrong prices mean lost spend control; OCR
and figure descriptions can be wrong; citations are references, not proofs; the answer cannot see edits
that have not synced from a browser; a published answer keeps its text after the source is removed;
chunk and figure caps can leave parts of a large file unanalyzed (always stated in the result); diagrams
cannot be exported yet.

## 16. Not built

Google Drive as a source depends on a Drive connection that does not exist in the repository (deferred
by the owner). Phase 2 (anything that writes into a document) is described from section 17.


---

# ThreadForge AI (phase 2)

Phase 2 lets `@AI` propose changes to a document and, only when a person says so, applies them to the live
shared document. Sections 17 onward describe it; sections 1-16 are phase 1 and still hold.

## 17. Phase 2, checkpoint 1: the diagram node, the draft seed format, proposal tables, target capture

### 17.1 The `diagram` node (packages/editor-schema)

A **node** is a kind of block in the editor's document tree (paragraph, heading, table...). The new node
`diagram` is a block whose **text content is the Mermaid source**:

```json
{ "type": "diagram", "attrs": { "id": "<uuid>" },
  "content": [{ "type": "text", "text": "flowchart TD\n  A[Start] --> B[End]" }] }
```

Why text and not an attribute: the document is a **Yjs** document (a CRDT, a data structure that merges
edits from many people without a central lock). Yjs merges *text* character by character, so two people editing
the same diagram both keep their edits; an attribute is replaced as one value, so one of them would lose.
It also means `blockHash` (the content hash used to detect "this block changed since the AI looked at it")
covers the diagram like any other block.

Rules (`validateDocument`, the one validator used by the browser, the collaboration service and the tests):
top-level only (not inside lists, quotes or tables); at most `maxDiagrams` (25) per document; language is
`mermaid`; no marks inside; line feeds are allowed in a diagram's text and nowhere else; the source passes
`validateMermaidSource` (allowed diagram types, at most 20,000 characters and 500 lines, no `%%{init}%%`
directives, no `click`/`href`/`link`, no HTML, no URLs, no `javascript:`/`data:` schemes, no entities).

**Two policies, on purpose** (`DiagramPolicy`):

| Policy | Used for | What it checks |
|---|---|---|
| `strict` (default) | seeds (including AI drafts), imports, AI proposals | the whole list above |
| `structure` | snapshots of the LIVE document (`collab/src/documents.ts`) | location, language, size limits, no control characters; not the syntax rules |

Reason: a person typing a diagram passes through states the strict rules reject (an empty source, a `<` in a
label). If a snapshot used the strict rules, one half-typed label would mark the whole document
`needs_repair`, and new connections would be refused until it was fixed. The text can still never be
*drawn* unsafely: `SafeMermaid` (browser) applies the strict rules again before it hands anything to Mermaid,
then Mermaid's strict security level, a timeout, and DOMPurify. And nothing unsafe can *enter* through the AI
or a seed, because those use `strict`. This is a deliberate reading of "validate with the shared Mermaid
validator inside validateDocument": the validator is shared and strict by default, and only the live-snapshot
caller opts into `structure`.

Browser view (`frontend/src/editor/DiagramView.tsx`): the node view draws the picture with `SafeMermaid`
(redrawn 400 ms after typing stops). People who can edit get an **Edit source** toggle that reveals ProseMirror's
own content DOM for the text (always present, hidden when off); viewers get the picture only. The toolbar's
"Insert diagram" inserts a small valid starter diagram (an empty one would not be strict-valid).

Not covered: Mermaid chooses the layout itself, so large diagrams become cluttered (the request path will fail with
a "split it" message above the size limit); diagrams are **not** exported to DOCX/PDF (that needs a rasterization
step that does not exist).

The schema fingerprint (`packages/editor-schema/schema.fingerprint`) changed because a node was added; the node
is additive, so existing documents and seeds are unaffected.

### 17.2 `documents.seed_format` and AI drafts

Migration `0011_ai_proposals` (expand only; downgrade works, but refuses to run while an `ai_draft` document
exists because the older CHECK constraint would reject it) adds `documents.seed_format`:
`'interchange'` (default, every existing row) or `'tiptap'`. `GET /internal/collab/documents/{id}/seed`
(`services/collab.get_seed`) returns `{format, content}` using it; the collaboration service's
`buildSeed(format, content)` already supported `tiptap` (the operator-restore path), so a draft is seeded through
the same code: `adapt` (interchange only) -> `validateDocument` (strict) -> Yjs. `template_key` may now be
`ai_draft`. Interchange `schema_version: 1` cannot describe a diagram, which is why drafts are stored as
validated Tiptap JSON.

### 17.3 Tables

`ai_proposals` and `ai_proposal_applications` (columns in the migration docstring and
`backend/app/models/ai_proposal.py`). Database-enforced: an edit has a target document and a captured counter,
a draft has neither; the document is in the same room as the proposal (composite foreign key); a proposal's
result belongs to its own request (composite key through `ai_results (request_id, id)`); one proposal per
request; `resolved_at` is set exactly when the state is final; `applying` carries who and since when;
`published_document_id` is unique and only set on an applied draft; a receipt hash is exactly 32 bytes and each
proposal has at most one receipt. The manifest cleanup skips requests that still have a `pending` or `applying`
proposal ("a capture referenced by a pending proposal must not be pruned").

### 17.4 Target capture (backend/app/ai/edit)

* `units.py` / `units.ts`: positions inside a block's text are **UTF-16 code units**, the unit JavaScript strings
  and Yjs use. The text of a paragraph, heading or diagram is its inline children in order: text nodes
  contribute their characters (an emoji is two units), a hard break contributes one unit `\n`, an inline image
  contributes one unit U+FFFC, marks take no space. A boundary between the two halves of a surrogate pair is
  refused, never rounded. Both implementations pass the same cases (`test/fixtures/range-cases.json`).
* `target.py`: `resolve_edit_target` turns what the person pointed at (selected block ids + the Phase 1
  selection hash, character ranges `(block_id, start, end, text_hash)`, or `insert_at: start|end`) into an
  `EditTarget`. **No target is `target_required`**: the model is never asked to guess. A missing block, a
  changed hash or a range that no longer resolves is `selection_changed`. The target records the scope (selected
  blocks and everything inside them), the declared ranges, insertion anchors (always **top-level** blocks, plus
  `start`), a block hash for every block in scope, a copy of each outermost selected block ("before"), and the
  captured `yjs_update_counter`.
* `linearize` shows the model `[B12] (paragraph) text` with short labels; the server maps labels back to ids.
  The model never sees an id, a hash, HTML or Yjs bytes. For a document over 40 blocks a table of contents of
  headings is added.


## 18. Phase 2, checkpoint 2: proposal generation and preview (no apply)

### 18.1 The flow, with the files

```
browser composer ──POST /rooms/{room}/ai-requests (operation edit|draft)──▶ services/ai_requests.create_request
        (admission: role, can_write, consent, keys, rate limit, capacity, target present)  → row + chat message
worker  answer/execute.py _work ──(operation != "answer")──▶ edit/execute.py run
        gate again → capture the document ONCE (collab /internal/ai/read) → resolve the target (edit/target.py)
        → retrieve evidence from the pinned files → manifest → model call (edit/generate.py)
        → normalize (edit/schema.py) → candidate + checks (Node: packages/proposal-check) → diagrams → ≤1 repair
        → ONE transaction (services/ai_requests.publish_success): ai_results + ai_proposals + chat reply
browser  reply card → GET /rooms/{room}/ai-proposals/{id} → review card with Preview (frontend/src/ai/Proposal.tsx)
```

A **proposal** is a request to change something, never a change. Where state lives at this checkpoint:
**PostgreSQL** (`ai_requests`, `ai_results`, `ai_proposals`, the manifest) and nothing else. The collaboration
process and the browsers' Yjs documents are only **read** (the capture); generating and previewing never write to
them, and a finished job never changes the room's active document.

### 18.2 Requests

`POST /rooms/{room_id}/ai-requests` gains `operation: "answer" | "edit" | "draft"`:

| Field | answer (phase 1) | edit | draft |
|---|---|---|---|
| `document_id` | required | required (the target document, even if not the open one) | **absent** (anchored on the room role) |
| `document_scope` | none / whole / blocks | `"edit"` | `none` |
| target | n/a | `selected_block_ids` + `selection_hash`, and/or `selected_ranges`, and/or `insert_at: start\|end` | n/a |
| files | optional | optional | **at least one** |

* **No target = no request** (`422 edit_target_required`): the person is asked to select blocks or a range, or to
  choose an insertion point. Natural language never changes the target: it is chosen by buttons and sent as data.
* `selected_ranges` items are `{block_id, start, end, text_hash}` in UTF-16 units of the block's text (17.4) with the
  SHA-256 of the selected text taken by the client at send time. At capture a missing block, a changed hash or a
  range that no longer resolves fails the request with `selection_changed` ("select again").
* A **draft** needs `owner` or `editor` and the AI gate, but no document: a room with no documents can start one.
  The analysis a draft waits for is anchored on no document (`analysis_jobs.anchor_document_id` is nullable); the
  worker still re-checks role, consent and switches.
* `AI_PROPOSALS_ENABLED` (default **false**) switches the whole feature; `/ai/status` reports `proposals_enabled`,
  and the composer offers the two outputs only when it is true. `AI_PROPOSALS_PER_USER_PER_HOUR` (20) limits
  requests per user (`429 proposal_rate_limited`). The normal per-user and per-room active-request caps apply.
* Permissions are Phase 1's, re-checked at admission, at the worker's claim, and **before publication**
  (a role or write-access loss while the model thinks fails the request and publishes nothing).

### 18.3 What the model sees and returns

It sees labels, never ids or hashes: `[B12] (paragraph) text` for the changeable blocks, a table of contents for a
document over 40 blocks, the declared ranges with their exact `start`/`end`, the offered insertion points
(`after [B3]`, `start`), and the evidence of the selected files (`P1`, `S2`, ...), all inside `UNTRUSTED` markers
with a random token. Prompt version `edit-v1` (recorded in the manifest).

Edit reply (`document_edit` schema, `edit/schema.py`): `{summary, operations[], evidence_gaps[]}`. Operations
(the typed form, executed by `packages/editor-schema/src/ops.ts`):

| Operation | Fields | Rules |
|---|---|---|
| `replace_block_text` | `block`, `new_inline[]` | a paragraph, heading or diagram inside the selection; keeps the element, id, alignment, spacing, indent |
| `replace_range` | `block`, `start`, `end`, `new_inline[]` | inside a **declared** range (or a wholly selected block); UTF-16 units; never inside a surrogate pair |
| `insert_blocks_after` | `anchor` (`B#` or `start`), `blocks[]` | the anchor must be an **offered** top-level block; new blocks are top-level |
| `delete_blocks` | `block_refs[]` | blocks inside the selection |

`new_inline` is a list of runs `{text, marks[]}`; marks are `bold`, `italic`, `underline`, `strike`, `link` (the
document link policy: http, https, mailto). Replaced text inherits the **font, size, colour and highlight** marks
of the first original run (of the block or of the replaced range); the model's marks are added. New blocks use
defaults. A new block is one of `paragraph`, `heading` (1-3), `quote`, `bullet_list`/`numbered_list` (flat items with
`depth`, at most 3 levels), `table` (existing row/column limits, optional header row), `diagram` (Mermaid source).
No images, fonts or colours from the model. Each operation carries `reason` and `citations`.

Draft reply (`document_draft` schema): `{title, summary, blocks[], include_sources_section, evidence_gaps[]}`, each
block with `citations`. If `include_sources_section` is not false the **server** appends "Sources" and a list of the
file names and locators actually cited (from the validated citation table, never from what the model says).

### 18.4 Validation, in order (nothing is previewed until all of it passes)

1. **Shape** (`edit/schema.py`): types, lengths, counts; unknown operation/block/mark = error; labels must be ones
   that were shown. At most **50 operations**, **30,000 inserted characters** per proposal.
2. **Citations** (`CitationTable`, shared with answers): labels map to the evidence bundle; quotes must be in the
   passage and are dropped on OCR/vision/figure items; chat is context, not evidence. When the request has files and
   more than 30 % of the content-changing operations (blocks, for a draft) have no valid citation, that is a defect.
3. **The candidate document** (`packages/proposal-check`, a sandboxed Node child running the SAME engine the
   collaboration service uses at apply time): scope (selection, ranges, anchors), one operation per block, links,
   list depth, table shape, Mermaid rules per new diagram, then the whole result through `validateDocument`.
4. **Diagrams**: size (`AI_DIAGRAM_SPLIT_CHARS`, default 8000, else "split it into smaller diagrams") and the Mermaid
   parse (`packages/mermaid-check`).

A defect in any step costs **one repair**: the problems (by operation number) are fed back in one more model call
(metered like any other). A second failure fails the request visibly: `proposal_invalid`, with the problem codes in
`error_detail`; nothing is published, no partial proposal ever exists. If Node or the engine is missing the request
fails `proposal_check_unavailable` (retryable): a proposal is never published unchecked.

### 18.5 Stored and shown

`ai_proposals` (state `pending`) holds the validated `operations` (JSONB), a copy of every target block's **before**
content (`before_content`, so snapshot pruning cannot remove it), the **base hashes** of every block in scope, the
declared scope, the captured `yjs_update_counter`, the citation table, and the **preview** (for each change: the
blocks before and after, the model's reason, citation numbers; for a draft, the whole validated document). The
manifest of the request is kept while its proposal is pending or applying. `GET /rooms/{room}/ai-proposals/{id}`
serves it to the requester and to owners/editors with **current** write access to the target (for a draft: the
requester and the owner); anything else is `403`, another room's proposal is `404`. Expired (`AI_PROPOSAL_TTL_DAYS`,
7) proposals stay readable and are reported as `expired`. Deleting the reply card (owner only) rejects a pending
proposal and wipes its content.

The review card (`frontend/src/ai/Proposal.tsx`): the target document named (even if it is not the open one), the
summary, "this is a preview, nothing has changed", **Preview** (a word-level diff for text changes, the blocks before
and after, rendered diagrams through SafeMermaid, the reason and citation marks that open the cited passage). There
are **no Apply/Reject buttons yet**: they arrive with checkpoint 4, with their backend.

### 18.6 The evaluation gate (the owner's brief, section 0)

`RUN_EVAL=1 uv run pytest tests/eval/test_proposal_eval.py -s` (stub) and
`RUN_EVAL=1 EVAL_PROVIDER=real ...` (the configured model) run 16 gold items (13 edits, 3 drafts) through the real
pipeline and write `tests/eval/results/proposals-<mode>-results.md`: schema-valid rate **without** and **with one
repair**, the share of proposals whose base hashes, anchors and replay resolve against the captured document,
citation validity, and a sample of previews. Real mode asserts the 90 % line (it fails the run below it).

**Result: UNMEASURED.** No provider credentials exist here, so only the stub mode ran. Its numbers (62 % valid
without repair, 94 % with one repair, 100 % resolving, over a *scripted* stand-in with deliberate defects in 6 of
16 items) show that the harness, the repair loop and the engine work; they are **not** a measurement of any model and
cannot pass or fail the gate. The owner chose to have phase 2 built first and supply keys afterwards, so checkpoints
3-5 were built **before** the gate could be measured. If the first real run is below 90 %, the apply path built here
should not be shipped until the generation quality is fixed.

### 18.7 Unverified at this checkpoint

* Nothing here has talked to a real model: reply shapes under a provider's strict structured-output mode (nesting,
  unions with `null`) follow the documented limits but were not exercised.
* The preview shows blocks as stored; it is not a pixel-accurate rendering of fonts and spacing.
* No cost estimate is shown for proposals (the estimate route knows answers only).
* The Node engine runs under the same scrubbed-environment child policy as the Mermaid check, without the OS-level
  resource limits on Windows.


## 19. Phase 2, checkpoint 3: applying a proposal to the live document

### 19.1 Why one process can do this atomically (and what it cannot do)

The collaboration service (`collab/`, Node) holds the live **Y.Doc** of every open document and handles every update
to it on a single thread. Yjs is a CRDT (a data structure that merges edits from many people without a central
lock). A **synchronous Yjs transaction** (`doc.transact`) is therefore atomic against every update the service
has *already received*: no other update can interleave inside it. This design is for **one collaboration instance**
(the lease guarantees that); there are no document epochs and no freezing.

What it cannot see: an edit still sitting **unsent in a browser**, or made **offline**. That edit reaches the server later, and
if it is inside a block the AI replaced it is dropped (see 19.6). Everything below is built around being exact about that boundary.

### 19.2 The protocol (`collab/src/apply.ts`), step by step

`POST /internal/ai/apply-proposal {proposal_id, document_id, user_id}` (secret-protected, private network; the API's
public apply route calls it, 20.1):

| # | Step | Where it is decided | If it fails |
|---|---|---|---|
| 1 | **Lease**: this instance still holds the fenced lease | collab `LeaseKeeper`; the token is sent with every internal write and checked INSIDE the database transaction | `409 stale_lease`, nothing changed |
| 2 | **Authority**: the user may write the document *now* (role, membership, document state); the proposal is an edit of THIS document and room, `pending` (or `applying` by the same user), unexpired; then `pending -> applying` by **compare-and-set** | PostgreSQL, `ai_proposals.begin_apply`, under the room lock | `403`, `404`, `409 proposal_<state>`; two competing applies: exactly one wins |
| 3 | **Quiet**: if a *person's* edit reached the document within `AI_APPLY_QUIET_SECONDS` (default 3), wait up to `AI_APPLY_MAX_WAIT_SECONDS` (10) | collab, `lastHumanUpdateAt` (server-side changes do not count). **Document-level, not block-level.** | `document_busy` (retryable); the proposal goes back to `pending` |
| 4 | **Check in memory, before any mutation**: read the live document; recompute the hash of every target block (`blockHash`, the shared package) and compare it with the proposal's `base_hashes`; run the SAME engine the worker used (`applyOperations`) on the live JSON to build the candidate; validate the whole result (`validateDocument`); **rehearse** the Yjs change on a *copy* and require the identical document | collab + `packages/editor-schema` | `409 conflicted` (`block_changed`, `block_missing`, `candidate_*`): **nothing changed**, the proposal becomes `conflicted` |
| 5 | **Commit**: ONE `doc.transact` with its own origin `{source: "local", context: {source: "ai-apply", ...}}`: all changes together, plus an **idempotency marker** in the root `Y.Map` `ai_applications` keyed by the proposal id. A marker that already exists means "already applied": no change, same outcome | collab, `yapply.ts` | the transaction is all-or-nothing |
| 6 | **Durable**: store NOW (not debounced). The *application receipt* (`ai_proposal_applications`: who, the resulting counter, a receipt hash) is written in the **same PostgreSQL transaction as the Yjs bytes** (`PUT /internal/collab/documents/{id}/state` with `X-Proposal-Id`, `X-Applied-By`, `X-Receipt-Hash`). Only after that commits is the proposal marked `applied` and the HTTP call answered | PostgreSQL | see the failure table |
| 7 | **Recovery**: a sweeper at service start and every `COLLAB_AI_SWEEP_MS` looks for proposals stuck in `applying` | collab `ProposalApplier.sweep` | see below |

Between 4 and 5 nothing may reach the document: the state vector is read before the checks and compared with the one
just before the transaction (hashing is asynchronous, so a message could slip in); any change restarts the check, at
most three times, then `document_busy`.

Replacing text keeps the block **element** (its id, alignment, spacing, indent): only its inline children are
deleted and re-inserted. Only `delete_blocks` removes elements. New blocks are built by `prosemirrorJSONToYXmlFragment`,
the library the editor itself uses. Block ids, new content and order all come from the engine's result (`changes`);
`yapply.ts` decides nothing.

### 19.3 Where state lives

| State | Where |
|---|---|
| the document people edit | each browser's Y.Doc, and the collaboration process's live Y.Doc (they sync over the WebSocket) |
| the proposal, its operations, hashes, before-copy, the receipt | PostgreSQL (`ai_proposals`, `ai_proposal_applications`) |
| the durable document | PostgreSQL (`document_yjs_state`, the full Yjs state, with a counter) |
| "which proposals were applied to this document" | inside the document itself: the `ai_applications` Y.Map (so it survives crashes and travels with reconnecting clients) |
| "is this proposal being applied right now" | the proposal's `applying` state in PostgreSQL |

### 19.4 Failure outcomes

| What happens | Result |
|---|---|
| a target block changed or vanished since the proposal | `conflicted`, document unchanged, **Regenerate** offered |
| any operation cannot be applied, or the result fails the document rules | whole proposal refused (`conflicted`, `candidate_<code>`); never a partial change |
| people are typing | `document_busy`, retryable; proposal stays `pending` |
| the lease is stale | `stale_lease`, nothing changed |
| the user lost write access (or was removed) between request and apply | `403`, proposal stays `pending`, nothing changed |
| the proposal expired / was rejected / already applied | `409 proposal_<state>`; "already applied" is reported, not repeated |
| the service **crashes or the store fails AFTER the change was broadcast** | the connected editors hold the change *and* the marker; the proposal stays `applying`; the sweeper stores the document with the receipt and marks it `applied` (or, if the process restarted and no client brought the change back, finds no marker and returns it to `pending`) |
| the service crashes **after the store committed but before the proposal row was updated** | the receipt row already exists; the sweeper marks the proposal `applied`; there is no second change (the marker makes a repeat a no-op) |
| the same proposal is applied twice, or at once | compare-and-set + the in-process single-flight + the marker: **one change** |

"Synced" and "durable" are reported as separate facts: the reply says `synced: true` (every connected editor already has
the change) and `durable: true` (it is in PostgreSQL with its receipt). If the store fails, the card shows "Being
applied: the change has reached the editors and is being saved" (state `applying`) until the sweeper finishes it.

### 19.5 Undo

A person's Ctrl+Z must not undo an AI change, and the AI must not undo a person's. The editor's undo manager
(y-prosemirror's `yUndoPlugin`) tracks only transactions whose origin is the editor's own sync plugin key. The AI's
change happens in the *server's* Y.Doc with origin `{source: "local", context: {source: "ai-apply"}}` and arrives in
each browser as a **remote** update applied by the provider, whose origin is not tracked. Tested three ways: the
server-side origin carries `ai-apply` and is not among a manager's tracked origins; a `Y.UndoManager` that tracks only
local typing leaves an applied AI block alone (`collab/test/apply.test.ts`); and two real browsers
(`frontend/e2e/ai-proposals.spec.ts`).

### 19.6 Limits, stated plainly

* A **base hash protects the targeted blocks** against edits the server has seen. It does **not** protect against edits
  still unsent in another browser or made **offline**.
* **An unsent edit inside a block the AI replaced is lost.** Replacing text deletes the block's old text node and
  inserts the new one (the block element and its id stay). Yjs discards anything a client inserted *inside* a node that
  was deleted when that client's change finally merges. Measured in `collab/test/apply.test.ts` ("documents what an offline
  client's UNSENT edit inside a replaced block does"): a person who typed offline at the end of "Bravo", while the AI
  replaced "Bravo", comes back and every side converges on the AI's text; their typed words are gone, with no warning to
  them. (This corrects an earlier expectation that the words would survive next to the AI's text: they do not.) The same
  holds for a block removed by `delete_blocks`.
* So the real protection is the **base hash + quiet guard + confirm dialog** (everything the server has seen), and the
  reviewer's own judgement for people who may be offline. Nothing in this design can detect an edit the server never received.
* The quiet guard is **document-level**: while anyone is typing anywhere in the document, an apply waits.
* The presence warning in the confirm dialog uses presence of the document **open in this browser**; for a proposal
  about another document, the reply reports how many people were connected afterwards.
* One collaboration instance; no multi-instance scaling; no offline-recovery beyond what is described here.
* The `ai_applications` marker map is never pruned (one small entry per applied proposal).

### 19.7 Internal API (`/internal/collab/...`, secret + fenced lease)

`POST /proposals/{id}/begin-apply` (checks and compare-and-set; returns operations, base hashes, scope),
`POST /proposals/{id}/finish-apply`, `POST /proposals/{id}/release-apply` (-> `pending`), `POST /proposals/{id}/conflicted`,
`GET /proposals/stuck?older_than_seconds=N`, and `PUT /documents/{id}/state` with the receipt headers. Each takes the
fencing token and checks it inside its own transaction. None is reachable from a browser; none carries document text.

## 20. Phase 2, checkpoint 4: review, apply, reject, regenerate, expiry, events, publishing a draft

### 20.1 Routes (`backend/app/api/ai_proposals.py`)

| Route | Who | What |
|---|---|---|
| `GET /rooms/{room}/ai-proposals/{id}` | requester; owner/editor with current write access (draft: requester and owner) | the proposal, its preview and `can_apply`, `can_reject`, `can_regenerate`, `can_publish` for THIS person |
| `POST .../apply` | same people, and write access *now* | asks the collaboration service (19.2); returns `{state, already, synced, durable, counter, connected_users}` |
| `POST .../reject` | the requester, or anyone with write access (draft: requester or owner) | `pending -> rejected`; changes nothing else; idempotent |
| `POST .../regenerate {client_request_id}` | same people as apply, only from `conflicted` | a NEW request with the same instruction, files and target blocks, read from the CURRENT document; the old proposal becomes `superseded` and stays readable |
| `POST .../publish {client_request_id}` | the **owner** | creates the draft's document through the existing create-document path and makes it the active document |

All state-changing routes are CSRF-protected, return `404` for another room's or a missing proposal, and are
whole-proposal actions only. Errors the card maps to words: `proposal_conflicted`, `document_busy`, `proposal_expired`,
`proposal_rejected`, `proposal_applied`, `proposal_applying`, `proposal_superseded`, `ai_write_access_required`,
`collab_unavailable`.

### 20.2 States

`pending -> applying -> applied | conflicted | pending`; `pending -> rejected | expired`; `conflicted -> superseded`;
a draft goes `pending -> applied` when published. Only `pending` can be rejected, expired or superseded, so an apply in
flight is never pulled out from under the collaboration service. A pending proposal past `AI_PROPOSAL_TTL_DAYS` is
*reported* as `expired` immediately and *made* expired by the AI worker (an event is recorded). An expired proposal cannot
be applied or published but stays readable until retention.

### 20.3 Events

`ai_proposal_updated {proposal_id, state}` (migration 0013; type CHECK, payload schema in `schemas/events.py`, client
handling in `client/src/room-client.ts` -> `AiSignals.proposals`, the frontend store refetches the authorized view, and the
completeness audit lists the writers). Recorded in the same transaction as every state change, at creation (`pending`),
`applying`, `applied`, `conflicted`, `rejected`, `expired`, `superseded`. **Document changes themselves do not travel as
events**: they reach clients through the Yjs connection.

### 20.4 Publishing a draft

Owner only. `documents.create_document` gained two hooks that run INSIDE its transaction: `before_insert` re-checks the
proposal (draft, pending, unexpired) and `after_insert` links it (`published_document_id`, state `applied`,
resolution `published`) and records the proposal event, so *document + `document_created` + `active_document_changed`
+ proposal update + proposal event* commit together or not at all. The content is the draft's validated Tiptap JSON
(`seed_format = tiptap`, `template_key = ai_draft`), seeded by the collaboration service through the same path as every
document (strictly validated again, diagrams included). A retry with the same `client_request_id` finds the document it
made (the request fingerprint includes the proposal id); a different id finds the proposal already published
(`409 proposal_applied`). The confirm dialog says "this will become the active document". A draft is never published
automatically, and an editor's draft waits as a pending card until the owner publishes or rejects it.

### 20.5 Retention

Resolved proposals keep their before-copy, preview and receipt for `AI_PROPOSAL_RETENTION_DAYS` (30) after resolution, then
`python -m app.commands.cleanup_ai` deletes them; `pending` and `applying` proposals are never deleted, and the request
manifest behind a pending proposal is kept (17.3). Deleting the answer card (owner only) rejects a pending proposal and
wipes its content.
