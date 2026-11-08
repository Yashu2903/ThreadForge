# Documents (Part 6): progress

Resume here in a new session. Commits are one per checkpoint; run `git log --oneline` to see them.
Specification: the Part 6 task statement (documents, custom editor, DOCX import, Google Drive).

## Checkpoint status

| # | Checkpoint | State |
|---|---|---|
| 1 | Schema, migrations, shared schema + golden fixtures, validators, templates, document APIs, active selection, events, Part 5 integration | **done** |
| 2 | Frontend shell: room layout, document list and dialogs, minimal chat, capabilities endpoint | **done** |
| 3 | Editor core: model, renderer, selection mapping, beforeinput, composition, typing, Enter/Backspace/Delete, paste, undo/redo | next |
| 4 | Formatting and structure, toolbar, accessibility | not started |
| 5 | Assets and images: pipeline, storage, quotas, editor insert | not started |
| 6 | Saving and conflicts: autosave, serialization, event-driven refresh, conflict UI, read-only, access loss | not started |
| 7 | Imports: worker, local upload, conversion, report, isolation, publication | not started |
| 8 | Google Drive: connection, Picker, import (stub-tested; real verification pending) | not started |
| 9 | Integration tests, browser tests, documentation, final report | not started |

## Checkpoint 1: what is done

* `backend/app/documents/` (`schema.py` validator + canonicalizer, `canonical.py`, `templates.py`),
  `client/src/document-schema.ts`, `shared/fixtures/` (76 golden files), `docs/document-schema.md`.
* Migration `0005`; models `documents`, `room_assets`, `document_asset_refs`,
  `document_write_buckets`; `rooms.active_document_id/version`; four room event types.
* `services/documents.py`, `api/documents.py`, `schemas/documents.py`, per-route body limit rules
  (`app/body_limit.py`), error constructors, settings (`DOCUMENT_*`).
* Part 5 client: `DocumentSignals`, `onDocumentSignals`, selection monotonicity, `reset` after a
  bootstrap. Part 4 completeness audit extended to the document services.

## Verified (automated)

See the commit message and the README Part 6 section for the commands. Backend: `tests/test_document_schema.py`
(133, golden fixtures + properties), `test_documents_api.py` (36), `test_documents_save.py` (37),
`test_documents_constraints.py`, existing suites adjusted for the new room fields. Client:
`test/document-schema.test.ts` (108), `test/document-events.test.ts` (8), all Part 5 tests unchanged.

## Not built yet / blocked

* No frontend exists yet (`frontend/` is created in checkpoint 2). The existing `client/` is the
  framework-free room client, reused from React.
* `room_assets` rows can only be created by tests until checkpoint 5 (the upload pipeline).
* `documents.requires_review` is a constant `false` until imports exist (checkpoint 7 joins
  `document_imports`); no import can exist before then, so it is truthful.
* Tables for imports (`document_imports`), cloud connections and provider auth attempts come with
  checkpoints 7 and 8; `documents.import_id` and `room_assets.import_id` get their foreign key then.
* Real Google credentials are not available here: checkpoint 8 will be stub-tested and every
  real-provider check marked pending.

## Decisions worth remembering

* The validator reports the first error only (fixed traversal order shared by both languages).
* Save order inside the lock is room → document → assets → rate-limit bucket; a stale or unchanged
  save never consumes quota.
* `resource_version` on room events is an INTEGER column; `content_version` is BIGINT. A document
  would need over 2 billion saves to matter; noted, not changed.

## Checkpoint 2: what is done

* `frontend/` (React 18, TS strict, Vite, vitest, Playwright): `api.ts` (cookies, CSRF with one
  retry on `csrf_failed`, error envelope), `documents/store.ts` (framework-free list + open document,
  If-None-Match, 1 s refetch throttle, stale-answer discard), `useRoom.ts` (ONE `RoomClient` per room,
  its `onDocumentSignals` feeds the store), components (`Dialog`, `StartDialog`, `OpenDialog`,
  `ChatPanel`, `DocumentRead`), `RoomView`, `App` (hash routes).
* Backend: `GET /capabilities`, Drive settings (`google_drive_*`, `google_picker_api_key`,
  `google_cloud_project_number`, `cloud_token_encryption_keys`; `drive_configured`),
  `scripts/seed_session.py` (test-only), `scripts/e2e_prepare.py` (test-only).

Verified: `npm run typecheck`, `npm test` (33 tests: store, dialogs, read-only renderer, API client),
`npm run build`, and `npx playwright test` (2 tests in Chromium against the real API, dispatcher,
Redis and Vite: owner starts a template document; a viewer sees it read-only; chat works in both
directions live; the owner creating a second document moves the viewer's active document live;
removing a member shows "You no longer have access" without a reload). Backend:
`tests/test_capabilities_and_seed.py` and a full backend run (1190 passed before the capabilities
tests were added).

Left out on purpose until their backend exists: "Upload from this device" (checkpoint 7), the Drive
picker (checkpoint 8). Commit note: the small capabilities/Drive-settings backend pieces landed with
the checkpoint 1 commit because `config.py` and `main.py` changed in both.

## Next step

Checkpoint 3: the editor core, as a framework-free island under `frontend/src/editor/` (model,
renderer, selection mapping, beforeinput pipeline, composition handling, typing, Enter, Backspace/
Delete, plain-text paste, undo/redo), mounted from React through a ref and an imperative interface.
