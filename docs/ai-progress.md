# AI phase 1 progress

One section per checkpoint, one commit each. Design and reference: `docs/ai.md`.

## Baseline (before any AI change)

Last full runs on this repository state (collaboration checkpoint 2, commit `616220f`):

| Suite | Result |
|---|---|
| backend pytest | 1284 passed (four sequential batches; one single full run is reaped by the host for low memory) |
| collab service vitest | 28 passed |
| editor-schema package vitest | 91 passed |
| frontend vitest | 192 at collaboration checkpoint 1 (not re-run since; nothing it covers changed) |
| `client/` vitest | 177 (not re-run since) |
| Playwright | 69 passed, 2 skipped (not re-run since) |

Not re-run for AI checkpoint 1 because no frontend or `client/` code changed.

## Checkpoint 1: providers, keys, consent, switches, can_write, spend

**Done**
* Migration `0007_ai_foundations` (expand only; working downgrade; `alembic check` clean): `rooms.ai_enabled`,
  `ai_global_flags`, `ai_consents`, `user_provider_keys`, `ai_capability_status`, `ai_usage_ledger`,
  `ai_budget_windows`, `ai_budget_warnings`, `ai_rate_buckets`, and the `room_ai_settings_changed` event type.
* `app/ai/`: adapters (OpenAI Responses generation, Gemini embeddings, plain HTTP), call policy with retry and
  per-key concurrency, ledger, atomic budget reservation, warnings, caps, AES-GCM key sets with AAD, BYOK
  service, key policy, consent notice, self-check, capability status, estimate, usage aggregation.
* `app/services/permissions.py` (`decide_write`) used by the AI gate and the collaboration authorize endpoint.
* Routes: status, consent, keys, room AI settings, usage, estimate. Event `room_ai_settings_changed`.
* Operator commands: `ai_disable`, `ai_enable`, `ai_status`, `ai_spend_report`, `ai_selfcheck`;
  worker entry `python -m app.workers.ai_worker` (self-check only in this checkpoint).
* Settings with startup validation, `.env.example`, `docs/ai.md`.

**Verified (automated)**
* backend 1390 passed in sequential batches: 272 + 691 + 423 + 4 (baseline 1284; **106 new**: 41 core, 40 API,
  23 config, 2 migration), 0 failed in the final state. One existing guard test
  (`test_only_the_message_service_and_cleanup_touch_the_message_tables`) failed twice on my code and caught real
  layering mistakes (a direct read of `messages` from the estimate, then a docstring mention); fixed by
  moving the read into `services/messages.py`.
* Existing tests changed: `test_room_events.py` (13 event types; the completeness audit lists
  `set_room_ai_enabled`), `tests/db_utils.py` (AI tables truncated between tests).
* `ruff check`, `ruff format --check` clean on `app scripts tests`.
* Behaviour proven against real PostgreSQL and the real adapters over HTTP-level stubs: retry/no-retry
  classification, `Retry-After`, ledger rows for ok/failed/retried/cancelled, reservation under concurrency
  (no overshoot), rollback of partial reservations, warnings once, user-key exemption, caps, key encryption
  with AAD, no plaintext in DB/logs/responses, rate limiting of key validation, key policies without silent
  fallback, the permission table, `decide_write` reasons, shared collab authorize, kill switches and commands,
  self-check per capability, usage page, estimate.

**Not verified**: anything against the real OpenAI or Google APIs (see `docs/ai.md` section 8). The
self-check has not been run with real credentials.

**Blocked / adapted**: Google Drive, import jobs and the sandboxed converter do not exist in the repository
(see `docs/ai.md` section 1). The collaboration browser editor (collab checkpoint 3) is still pending.

**Next**: checkpoint 2 (done, see below).

## Checkpoint 2: sources (device uploads, inspection, policy, removal, cleanup)

**Done**
* Migration `0008_ai_sources` (expand only; working downgrade; `alembic check` clean): `sources`,
  `source_versions` (immutable), `source_uploads` (the intent and its bytes).
* Upload flow in three steps (intent / streamed bytes / finalize) with idempotency (`client_request_id` +
  keyed fingerprint), actual byte counting, signature checks, per-user rate, open-upload cap, room count and
  byte quotas that also reserve space for uploads in flight, replace-as-new-version, removal, download with
  `SOURCE_DOWNLOAD_ROLES`, list/inspect.
* Sandboxed inspector (`app/sources/inspect_child.py` + `sandbox.py`): child process with scrubbed
  environment, own empty working directory, wall-clock deadline, output cap, blocked sockets, and (Linux)
  address-space/CPU/file limits. PDF via `pypdfium2` (BSD-3-Clause / Apache-2.0), DOCX package checks with
  `zipfile` and `expat`, text checks.
* `python -m app.commands.cleanup_ai`: abandoned uploads, leftover and orphan files, stale scratch space.
* Settings `SOURCE_*` with startup validation, `.env.example`, `docs/ai.md` section 9, README command table.

**Verified (automated)**
* backend: **1594 passed, 2 skipped** in sequential batches (279 + 657 + 552 + 106; the 106 are the sources
  API file, run on its own); before this checkpoint 1359. 237 new tests: 48 (names, types, signatures),
  49 (inspector and sandbox; 2 are POSIX-only and skipped on this Windows machine), 106 (HTTP: the
  permission table on every route and role, flow, idempotency, hostile files, quotas under parallel
  requests, replace, removal, download, database constraints), 10 (cleanup), 24 (settings and migration).
  `ruff check` and `ruff format --check` clean on `app scripts tests`; `alembic check` clean; downgrade to
  `0007` and upgrade again work.
* collaboration service vitest: 64 passed (run as a regression check against the changed API).
  Frontend, `client/` and Playwright were not re-run: nothing they cover changed.
* Proven against real PostgreSQL, real storage and the real child process: the source modules import no
  provider code and no ledger row appears; logs never contain a file name or content; a demotion between
  steps is caught under the room lock; two finalizes at once make one source; the byte quota counts uploads
  in flight; cleanup racing finalize never loses a finished source; a failed file delete is remembered and
  finished by cleanup.
* Verified by hand in a Linux container (not part of the automated run): resource limits applied and
  enforced, sockets refused, writes refused (`docs/ai.md` section 8).

**Not verified**: the POSIX-limit test has not run in an automated Linux run; no fuzzing of PDFium; no
browser exercised the upload (there is no UI yet); no real storage volume shared between API and worker.

**Blocked / deferred**: Google Drive as a source (deferred by the owner; no Drive connection exists).
`source_updated` events, the passages inspect route, the source panel/attachment UI, redaction of stored
excerpts and dropping superseded versions are deliberately left to checkpoints 3-4 (reasons in `docs/ai.md`
section 9).

**Next**: checkpoint 3 (lazy analysis worker, OCR with vision fallback, figures, chunking, embeddings,
generations, hybrid retrieval, inspect view; switch Compose to a pgvector image and add Tesseract to the
worker image).
