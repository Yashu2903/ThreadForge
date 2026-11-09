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

## Checkpoints 3 and 4: lazy analysis, retrieval, `@AI` requests, answers, UI

These two were finished and committed together (see "Commit note" below).

**Done**
* Migrations `0009_ai_analysis` and `0010_ai_requests` (expand only; downgrade to `0008` and to base
  both work, upgrade again works; `alembic check` clean on a scratch database).
* Checkpoint 3: nothing is analyzed at upload. "Analyze" (or sending an `@AI` request that names a file)
  queues a job; a worker extracts text in a sandboxed child process (real Tesseract OCR with a quality
  gate, vision fallback), selects figures, chunks (`chunk-v1`), embeds, and publishes an index
  *generation* atomically. Hybrid retrieval (Postgres full-text + exact pgvector cosine, merged with
  reciprocal-rank fusion). Source removal cascades analysis data and redacts stored excerpts.
* Checkpoint 4: `@AI` requests (create, get, context, cancel, retry, result, citations, citation image),
  a claim/lease request queue, pinned generations, evidence manifests, a strict answer schema with
  server-side checks of citations, quotes and unsupported share (one repair attempt), Mermaid diagrams
  (static rules shared with the browser, a Node parse check, sanitized rendering), exactly one reply per
  request, new events (`ai_request_updated`, `source_updated`), and the browser UI (consent, source
  panel, `@AI` composer, request and answer cards, citation panel, viewer read-only view).

**Verified (automated)**
* backend: **1983 passed, 3 skipped** (the skips are POSIX-only sandbox checks and are not run on this
  Windows machine), run as 15 sequential batches because one process is killed for low memory here.
  Before checkpoint 3 the count was 1594.
* `ruff check` and `ruff format --check` clean; `alembic check` clean.
* editor-schema 126 passed, client 76, frontend 84 (vitest), collaboration service 64; typechecks clean.
* Browser (Playwright, chromium, real API + real AI worker + real collaboration service, with a local
  HTTP stand-in for the two providers): 2 passed. Covers notice acceptance, upload without analysis,
  "@AI" typed in a plain message triggering nothing, the mention menu, asking, the cited answer card,
  the citation panel, the viewer's read-only view, the message kinds, and owner deletion of an answer.

**Not verified**
* Nothing has run against the real Google or other providers (no credentials here). Answer quality, OCR
  accuracy on real scans, real latency and real cost are therefore unmeasured; the checkpoint 5
  evaluation harness is the place for those numbers.
* The POSIX resource limits of the new extractor child have not run in an automated Linux run.
* The Docker image (`backend/Dockerfile`, with Node and Tesseract) has not been built here (memory).
* Only chromium was run for the browser spec.

**Blocked / deferred**: Google Drive as a source (deferred by the owner). Phase 2 is not started.

**Commit note**: the brief asks for one commit per checkpoint. The two checkpoints share many files
(`config.py`, `ai_worker.py`, `services/sources.py`, `docs/ai.md`, the room client and the frontend), and
the checkpoint-3-only state was never run as a whole suite, so a split commit would claim a verification
that did not happen. They are one commit; the full-suite numbers above are for that commit.

**Next**: checkpoint 5 (evaluation harness and recorded results, security and regression sweep, final
report in `docs/ai.md`).

## Checkpoint 5: evaluation, security sweep, final report

**Done**
* `backend/tests/eval/` (not CI; `RUN_EVAL=1`): four synthetic sources (text PDF with eight look-alike
  pages, a three-page scan, a Word file with a figure, Markdown), a gold set of 13 questions (factual,
  figure, summary, comparison, document review, diagrams), metric functions (unit-tested in the normal
  suite: `tests/test_eval_metrics.py`, 10 tests), a runner and a report writer. Two modes: `stub` (run) and
  `real` (written, **never run**).
* `tests/test_ai_security_sweep.py` (13 tests): the AI HTTP surface equals a reviewed list of 31
  method+path pairs; every route refuses anonymous callers; an outsider cannot tell a real room from a
  missing one on any room route; an outsider's write attempts change no rows; no internal route in the
  public API; an import scan pins who may build provider adapters or use the metered gateway; every
  retrieval query filters by room and pinned generations inside the SQL; a removed source is unreachable.
* `docs/ai.md` sections 13-16 (evaluation and its results, the sweep, the final report with reading
  order, error codes, privacy notice text, values to confirm, unverified assumptions, limits), README
  command table.
* Lint: `E501` is now ignored for migrations and two scripts (long DDL strings), as it already was for the
  other long-prose modules.

**Verified (automated)**
* Stub evaluation: 13/13 published; retrieval recall@1/@3/@5 = 65% / 95% / 95%; every required phrase
  was in the evidence sent to the model (100%); OCR accuracy on clean synthetic scans 92% (real
  Tesseract); full section coverage on summaries/comparisons 100%. Model-dependent rows (citations,
  repairs, diagrams, tokens, cost, latency) are **stub values and not measurements of any model**; the
  report labels each row.
* New tests: 23 (10 metrics + 13 sweep) pass. No application code changed in this checkpoint, so the full
  backend run of the previous commit (1983 passed, 3 skipped) was **not repeated**; with the 23 new tests
  the expected total is 2006, not re-counted.
* `ruff check`, `ruff format --check` clean.

**Finding (not fixed)**: with look-alike pages, the full-text half ranks the correct page first only
about two thirds of the time because `ts_rank_cd` ignores word rarity. It is in the first three results
for 95% and always in the evidence. Whether real embeddings fix this is what the first `real` evaluation
must show.

**Not verified**: everything in `docs/ai.md` section 15 "Unverified assumptions": above all, no real
provider has ever been called; the `real` evaluation has not run; the Docker image has not been built;
only Chromium ran the browser spec; POSIX sandbox limits have not run in an automated Linux run.

**Blocked / deferred**: Google Drive as a source (deferred by the owner). Phase 2 not started.


---

# AI phase 2 progress

One section per checkpoint, one commit each. Design and reference: `docs/ai.md` sections 17 onward.

**Gate status (read this first).** The owner's phase 2 brief requires an evaluation of edit and draft generation
"with the configured model" after checkpoint 2, and says to stop if the schema-valid rate with one repair is
below 90%. No provider key exists in this environment, so that number cannot be measured here. The owner
said to build phase 2 first and supply the keys afterwards; the gate is therefore **unmeasured, not passed**.
It is recorded as such in every checkpoint below and in the final report. Stub numbers are labelled stub numbers.

## Phase 2 baseline (before any phase 2 change, commit `a10a5d8`)

Phase 1 final state: backend 1983 passed + 3 skipped at `36400b3` (23 tests added at `a10a5d8`, not re-counted),
editor-schema 126, `client/` 76, frontend 84, collaboration service 64, typechecks clean.

## Checkpoint 1: diagram node, seed format, proposal tables, target capture

**Done**
* Shared schema: the `diagram` node (text content = Mermaid source), strict and structure-only validation,
  `maxDiagrams`, adapter support, golden fixtures (`test/fixtures/diagrams.json`), pinned fingerprint updated,
  UTF-16 range helpers with a fixture shared with Python (`range-cases.json`).
* Browser: node view with SafeMermaid, "Edit source" for editors only, viewers see the picture, toolbar
  "Insert diagram"; styles.
* Collaboration service: live snapshots use the `structure` policy; seeding stays `strict`.
* Migration `0011_ai_proposals` (expand only): `documents.seed_format`, `template_key` gains `ai_draft`,
  `ai_proposals`, `ai_proposal_applications`, `ai_results (request_id, id)` unique. Upgrade, downgrade to `0010`,
  downgrade to base, upgrade again, `alembic check` clean (scratch database).
* `services/collab.get_seed` honours `seed_format`; manifest cleanup skips requests with a pending proposal.
* `backend/app/ai/edit/units.py`, `target.py`: range mapping, target resolution, linearization with labels.

* Also in this commit, ahead of its checkpoint: `packages/editor-schema/src/ops.ts` (the shared edit-operation
  engine: replace, range, insert, delete; formatting inheritance; scope; atomicity; caps), because the diagram
  block rules and the range mapping are what it is built on. It is documented under checkpoint 2.

**Verified (automated)**
* editor-schema **181 passed** (was 126: +17 diagram, +9 range mapping, +29 operations); frontend **88**
  (was 84: +4 diagram node view in a real Tiptap editor); collaboration service **69** (was 64: +5 seed format,
  diagram seeding, snapshot policy); `client/` 76 (unchanged). All typechecks clean.
* backend: the 14 files this checkpoint can affect (migrations, documents, collab internal API, cleanup, security
  sweep, room events, seeds, schema) plus the 39 new tests: **388 passed**. The **full** backend suite
  (1983 + 23 at baseline) was **not** re-run for this checkpoint; it is run in full at checkpoint 2.
* New tests: 15 in `test_ai_proposals_schema.py` (every database rule above), 24 in `test_edit_target.py`
  (shared range cases, scope, anchors, hashes, before copy, labels), 1 in `test_collab_internal.py` (seed format).
* `ruff check` and `ruff format --check` clean. Migration `0011`: upgrade, downgrade to `0010`, downgrade to base,
  upgrade again, `alembic check` clean (scratch database, dropped).

**Not verified**: nothing here talks to a model. The diagram node was exercised in jsdom (a real Tiptap editor)
and through Yjs, not yet in two real browsers (checkpoint 5). Undo behaviour for diagram typing is the editor's
normal text undo.

**Next**: checkpoint 2 (proposal generation, validation, review card with preview, no apply, and the gate report).
