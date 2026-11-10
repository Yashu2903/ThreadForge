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


## Checkpoint 2: proposal generation, validation, review card with preview (no apply), and the evaluation gate

**Done**
* Migration `0012_ai_proposal_requests` (expand only; the downgrade refuses to delete edit/draft history): `ai_requests`
  gains `operation` (`answer|edit|draft`), a nullable `document_id` (a draft is anchored on the room role),
  `document_scope = edit`, `selected_ranges`, `insert_at`; `task_type` gains `edit_proposal` and `draft`;
  `analysis_jobs.anchor_document_id` is nullable (the analysis a draft waits for has no document). Up, down to `0011`,
  down to base, up again, `alembic check` clean (scratch database, dropped).
* `packages/editor-schema/src/ops.ts`: the shared edit-operation engine (replace block text, replace range, insert
  after, delete; formatting inheritance; scope; one operation per block; caps; whole-document validation; a draft
  builder). `packages/proposal-check`: a sandboxed Node child that runs it for the worker.
* `backend/app/ai/edit/`: output schemas and normalization (`schema.py`), prompts `edit-v1`, generation with ONE
  repair (`generate.py`), the Node runner (`check.py`), the worker flow (`execute.py`, wired into the existing
  claim/lease/cancel machinery), and a target-less edit refused at admission (`edit_target_required`).
  `CitationTable` was extracted from `validate_answer` unchanged so proposals resolve citations exactly like answers.
* `ai_proposals` rows are born with the result and the chat reply in `publish_success` (one transaction); manifests
  behind a pending proposal are kept; deleting the card rejects a pending proposal and wipes it.
* `GET /rooms/{room}/ai-proposals/{id}`; `AI_PROPOSALS_ENABLED` (default false) and `proposals_enabled` in `/ai/status`;
  per-user rate limit; the composer's "Propose document edit" / "Draft new document" with an explicit target chooser
  (selected blocks, selected text as a UTF-16 range, start or end of the document); the review card with Preview
  (word diff, before/after, rendered diagrams, reasons, citations).
* The evaluation harness `tests/eval/test_proposal_eval.py` (16 gold items) and its recorded stub run
  `tests/eval/results/proposals-stub-results.md`.

**Where the interface is committed.** The composer's edit/draft choices and the review card were written together
with the Apply / Reject / Regenerate / Publish controls, so they are committed with checkpoints 3 and 4: no commit
contains a button its backend cannot serve. This commit is the backend, the shared engine, the checker and the harness.

**Gate (the owner's brief, section 0): UNMEASURED.** No provider credentials exist here. The stub run (a scripted
stand-in with deliberate defects in 6 of 16 items) gives 62 % schema-valid without repair, 94 % with one repair, 100 %
of published proposals resolving against the captured document, 100 % citation validity. These are numbers about the
harness and the repair loop, **not** about any model, and they cannot pass or fail the gate. The owner asked for
phase 2 to be built before supplying keys, so checkpoints 3-5 were built before the gate could be measured.

**Verified (automated)**
* backend, full suite, 15 sequential batches (run in two parts because the host killed the first run for low memory at
  batch 10): **2085 passed, 3 skipped, 0 failed**. Batches 1-8 passed (928, 2 skipped). In the first run batch 9 had ONE failure,
  `test_outbox_dispatcher.py::test_only_due_rows_are_claimed_up_to_the_batch_size` (expected 6 claimed rows, got 0). It passes alone (28 of 28)
  and its whole batch passed on a rerun (187); the cause was **not found** (code I did not change; it looks like interference
  from something else touching the outbox during that run). Treat it as an unexplained flake, not as proven harmless. Batches
  10-15: 970 passed, 1 skipped. New: 39 generation/permission tests (`test_ai_proposals_generation.py`), the security sweep updated for the
  new route (13), 24 + 15 from checkpoint 1.
* editor-schema 181 (29 operations), proposal-check 5, frontend 102 (14 new: composer choices, target data sent,
  review card, preview, citations), client 76, collaboration service 69. `ruff` clean; typechecks clean.
* Proven against real PostgreSQL, the real adapters and the real Node engine: a replacement/range/insertion/deletion
  becomes a pending proposal and the document is untouched; a wrong label, a wrong range, a hostile diagram and
  non-JSON are repaired once; two bad replies fail the request visibly and publish nothing; out-of-scope labels, undeclared
  anchors, javascript: links, ragged tables, over-large heading levels, image blocks, unknown operations, more than 50
  operations, more than 30,000 characters, bogus citations and a diagram above the size limit never become proposals;
  a target-less edit is refused; a changed selection is `selection_changed`; instructions inside the document cannot widen
  the scope; a draft works in a room with no documents; viewers/outsiders/read-only editors cannot ask; losing write access
  while queued or while the model thinks blocks publication; the per-user rate limit; both model calls of a repair are
  metered.

**Not verified**: anything against a real model (reply shapes under a provider's strict structured-output mode, the real
quality and cost of proposals); no cost estimate is shown for proposals.

**Next**: checkpoints 3 and 4 (the apply path, then apply/reject/regenerate/expiry/events/UI/draft publishing).


## Checkpoints 3 and 4: applying a proposal to the live document, and everything around it

Committed together, as in phase 1, because the apply protocol is only reachable through the routes and the review card,
and the review card was written together with its controls (see the note under checkpoint 2).

**Done**
* **The apply protocol** (`collab/src/apply.ts`, `yapply.ts`, `docs/ai.md` section 19): lease check, authority and a
  compare-and-set `pending -> applying`, a quiet guard, an in-memory check of every target block hash and a rehearsal of
  the exact Yjs change on a copy, one `doc.transact` with its own non-undo-tracked origin and an idempotency marker, a
  forced store whose receipt is written in the same PostgreSQL transaction as the Yjs bytes, and a sweeper that finishes or
  releases proposals left in `applying` by a crash.
* **The review side** (section 20): `apply`, `reject`, `regenerate` (from `conflicted`), `publish` (owner only, through the
  existing create-document path with `seed_format=tiptap`), expiry made by the AI worker, retention in `cleanup_ai`,
  the `ai_proposal_updated` event (migration `0013`, also `ai_requests.regenerate_of`), and the interface: composer
  choices, review card with preview, confirm dialogs, conflict and busy messages.
* Internal routes the collaboration service uses (`begin-apply`, `finish-apply`, `release-apply`, `conflicted`, `stuck`,
  receipt headers on the state PUT), each checking the fencing token inside its own transaction.

**Found by testing, and fixed**
* The rehearsal refused the first real apply: replacing text deleted the old text but inserted nothing (a detached Yjs
  element returns no children). The rehearsal did its job: nothing was changed. Fixed in `yapply.ts`.
* `python -m app.commands.cleanup_ai` crashed on start (its slim settings class lacked `ai_proposal_retention_days`).
  Caught by an existing test in the full run; fixed.
* The collaboration service started by the browser tests used a stale `collab/dist`; `npm run build` must be run
  before `npm run e2e` (the global setup already says so).

**A limit, measured rather than assumed.** An edit a person typed *offline or unsent* inside a block the AI replaced is
**dropped** when it later merges (Yjs discards content inserted inside a deleted node); everyone converges on the AI's
text and the person is not told. The same holds for `delete_blocks`. An earlier draft of the documentation said such text
would survive next to the AI's; a test showed it does not, and `docs/ai.md` 19.6 and `collab/test/apply.test.ts` now say
so. The base hash, the quiet guard and the confirm dialog protect only against what the server has seen.

**Verified (automated)**
* backend, full suite in 15 sequential batches (the host stopped two runs for low memory; every batch was then run to
  completion, some twice): **2109 passed, 3 skipped, 0 failed**. 24 new action tests (`test_ai_proposals_actions.py`:
  begin/CAS under competition, fencing, receipt in the same transaction, stuck listing, reject/regenerate/publish rules,
  retention); security sweep now lists the four new routes (outsider answers identical to a missing id; no login = 401/403);
  the events completeness audit lists every writer of the new event.
* Flaky runs seen once and **not explained**: `test_outbox_dispatcher` (checkpoint 2 run), `test_ws_access` revoked-session
  and `test_ai_proposals_schema` valid-proposal (each failed once in a loaded full run and passed alone and on a rerun of
  their batch), and one `make_proposal` worker drain that returned nothing. All four are timing-shaped; none reproduced.
* collaboration service **88** (19 new: converge, busy, conflict, all-or-nothing, rotated active document, stale lease,
  access, CAS, crashes and the sweeper, restart recovery, undo, the offline limit, the private route); frontend **111**;
  client **77**; editor-schema 181 and proposal-check 5 (unchanged since checkpoint 2). All typechecks and `ruff` clean.
* **Two real browsers** (`frontend/e2e/ai-proposals.spec.ts`, real API/worker/collab service/PostgreSQL, stub providers):
  apply an insertion with convergence and an undo that leaves the AI's text; a conflict that changes nothing and Regenerate;
  a diagram proposed, previewed, applied, and shown read-only to a viewer; a draft that waits for the owner and is
  published once. The 16 existing browser tests (`ai-chat`, `editor-collab`) still pass.
* Migration `0013`: upgrade, downgrade to `0012`, up, down to base, up, `alembic check` clean (scratch database, dropped).
  SQLAlchemy warns that `ai_proposals`, `ai_requests` and `ai_results` have a foreign-key cycle (it cannot sort them
  for autogenerate); nothing is wrong at run time.

**Not verified**: any real model; two browsers on Firefox (only Chromium was run); an apply under real network latency;
more than one collaboration instance (the design does not support it).

**Next**: checkpoint 5 (final numbers, the demonstration, the final report with unverified assumptions).
