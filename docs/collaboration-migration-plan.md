# Collaboration migration plan: custom editor and versioned saves to Tiptap + Yjs + Hocuspocus

This is checkpoint 1's inspection report and the plan the later checkpoints follow. It describes what
**actually exists** at commit `056fc9c`, not what the task statement assumed.

## 0. The most important finding: imports, the DOCX converter and Google Drive do not exist yet

The task says to keep "import jobs, worker, report, parser isolation, publication rules; Google Drive"
unchanged. In the repository those were checkpoints 7 and 8 of the *previous* plan and were **never
built**: there is no `document_imports` table, no import worker, no DOCX converter, no `cloud_connections`
or `provider_auth_attempts` tables, no Drive code, and `documents.import_id` has no foreign key.
What does exist and is kept: templates (Python), image assets (storage, quotas, lifecycle), the
`documents.import_id`/`room_assets.import_id` columns and the `template_key = 'imported'` CHECK.

Decision (no question asked, because nothing blocks the work): this migration proceeds with everything
that exists. The seeding path is built around the **interchange format** (what the Python validator
accepts and what templates, and later the converter, produce), so a future import worker only has to
publish interchange JSON into `documents.content` with `collab_state = 'legacy'`. Every requirement in
the task that mentions imports or Drive (text-conservation check after seeding an import, "imports
still work", Drive import) is **not testable and not claimed**; it is listed under "Blocked: nothing to
migrate" in docs/collaboration-progress.md and in the final report. Templates exercise the same seeding
path and are fully tested.

## 1. Baseline (before any change)

| Suite | Command | Result |
|---|---|---|
| Backend (real PostgreSQL + Redis) | `cd backend && uv run pytest` | 1251 passed; 2 timing-sensitive tests failed in one run made while I was running other CPU-heavy work and passed alone, 3 times each (`test_messages_history::test_a_message_committed_later_never_has_an_earlier_created_at`, `test_outbox_dispatcher::test_an_envelope_that_disagrees_with_its_row_fails`) |
| Room client (vitest) | `cd client && npx vitest run` | 177 passed |
| Frontend (vitest, jsdom) | `cd frontend && npx vitest run` | 189 passed |
| Browser (Playwright, real API + dispatcher + Redis + PostgreSQL) | `cd frontend && npx playwright test` | 69 passed, 2 skipped (Chromium-only tests skipped on Firefox), 0 failed |
| Lint | `uv run ruff check` / `npx tsc --noEmit` | clean |

## 2. What exists (inspection report)

**Backend** (`backend/app/`): FastAPI, async SQLAlchemy, Alembic head `0005`.
* Document routes (`api/documents.py`): `POST/GET /rooms/{room}/documents`, `GET/PATCH .../{doc}`,
  `PUT /rooms/{room}/active-document`, **`PUT .../{doc}/content`** (the save route to be removed).
  Services in `services/documents.py` (`create_document`, `list_documents`, `get_document`,
  `rename_document`, `set_active_document`, **`save_content`**, `_sync_asset_refs`,
  `_consume_write_capacity`).
* Tables (`models/document.py`): `documents` (content JSONB, content_hash, content_version,
  metadata_version, client_request_id, request_fingerprint, import_id), `room_assets`,
  `document_asset_refs`, `document_write_buckets`; `rooms.active_document_id/version`.
* Validators: Python `documents/schema.py` (interchange validator + canonicalizer), TypeScript
  `client/src/document-schema.ts` (twin), 76 golden fixtures in `shared/fixtures/{valid,invalid,canonical}`.
  Templates: `documents/templates.py` (5 templates, interchange JSON).
* Assets: `assets_pipeline.py`, `storage.py`, `services/assets.py`, `api/assets.py`,
  `commands/cleanup_documents.py`.
* Events: `document_created`, `document_renamed`, `active_document_changed`, **`document_content_updated`**
  (emitted by `save_content`, allowed by the `room_events.type` CHECK).
* Session/membership validation: `services/sessions.load_session`, `services/memberships.get_active_role`,
  `dependencies/rooms.py` (pre-check), `services/room_lock.lock_room`/`rooms.lock_room_and_actor`
  (authoritative), `services/realtime_access.check_access(session_ids...)` (batched, used by the room hub).
* Room WebSocket (Parts 4 and 5): `realtime/` (hub, connection, subscriptions, presence), Redis hint
  channel `{prefix}:room:{room_id}`. **Kept unchanged.**
* Compose: `compose.yaml` runs **PostgreSQL and Redis only** (no API, no worker, no reverse proxy).
  The dev setup is the Vite dev server proxying `/auth /rooms /capabilities /health` to uvicorn.

**Frontend** (`frontend/src/`): React 18 + Vite. Custom editor `editor/` (doc, inline, segment,
commands, format, structure, images, stats, history, dom, editor, fonts, Toolbar, EditorSurface),
`documents/session.ts` (save/autosave/409 state machine), `documents/store.ts` (list + open document),
`documents/upload.ts` (image upload), `DocumentEditor.tsx`, `RoomView.tsx` (editor for owner/editor,
read-only renderer for viewers/mobile, switch banner), `components/DocumentRead.tsx` (read-only React
renderer), `Playground.tsx` (dev only). The room client (`client/src/room-client.ts`) exposes
`DocumentSignals` (`onDocumentSignals`) and keeps `state.role` current.

**Tests:** backend `tests/test_document*.py`, `test_assets.py`, `test_room_events.py` (completeness audit
includes `save_content`); frontend vitest for the editor (`editor-*.test.ts`), `session.test.ts`,
`store.test.ts`; Playwright `e2e/editor-*.spec.ts`, `documents-saving.spec.ts`, `room-layout.spec.ts`.

## 3. Keep / Replace / New, adapted to what exists

**Keep unchanged:** permission matrix; document list, create, rename; active document and
`active_document_version`; asset storage, quotas, lifecycle, serving; templates and the Python
interchange validator; room events `document_created`, `document_renamed`, `active_document_changed`;
chat; the room WebSocket of Parts 4 and 5 and its client; `documents.import_id`.

**Replace:**
* the custom editor and toolbar (`frontend/src/editor/*`, `Playground.tsx`) with Tiptap;
* `PUT .../content` and `expected_content_version` (route, service `save_content`, tests, schemas);
* `documents/session.ts`, the autosave/409 UI and "Load latest" flow in `DocumentEditor.tsx`/`RoomView.tsx`;
* custom undo/redo (`editor/history.ts`) with Yjs undo;
* `client/src/document-schema.ts` as an *editing* authority (it stays only as a fixture twin until the
  final cleanup; the interchange validator in Python remains authoritative for templates/converter);
* `document_write_buckets` usage, per-save `document_content_updated` events (stop emitting; the type
  stays allowed by the CHECK and the client ignores it).

**New:** `packages/editor-schema` (shared extensions, schema, limits, `validateDocument`, `adapt`,
hashes, text); `collab/` (Hocuspocus service); `/internal/collab/*` routes; `document_yjs_state`,
`document_snapshots`, `collab_lease`, `documents.collab_state`; document-socket access enforcement;
AI-ready hooks; collaboration tests.

**Not applicable until imports exist:** "imports still work", the import text-conservation check on the
seeded document, import publication events (nothing changes there because nothing exists), Drive.

## 4. Technology decisions (verified, not assumed)

| Question | Answer | Source |
|---|---|---|
| Tiptap major | **v3.33.0** (all `@tiptap/*` pinned to the same version) | npm registry |
| Disable built-in history | StarterKit option **`undoRedo: false`** (v2 called it `history`) | tiptap.dev StarterKit docs |
| Unique block ids free? | **`@tiptap/extension-unique-id` 3.33.0, MIT**, with `filterTransaction` (use `isChangeOrigin` for collaboration) and a server-side `generateUniqueIds(doc, extensions)` helper | npm + docs |
| Collaboration default Y field | **`default`** (option `field`) | tiptap.dev Collaboration docs |
| Remote cursors | v3 package **`@tiptap/extension-collaboration-caret`** (v2 was `-cursor`) | npm |
| y-prosemirror | v3 uses **`@tiptap/y-tiptap`** (a fork) under the hood; the task's "y-prosemirror JSON helpers" are verified at checkpoint 2 | npm; checked again in cp2 |
| Hocuspocus | `@hocuspocus/server`, `@hocuspocus/provider`, `@hocuspocus/extension-throttle` **4.8.0**, MIT | npm |
| Unknown attrs/marks when loading JSON | unknown **marks and nodes throw**; unknown **attributes are silently ignored**: `validateDocument` compares input with the node's own JSON and reports `would_be_dropped` | verified by test (`validate.test.ts`) |
| Free license check | every package in the install tree is MIT/ISC/BSD/Apache except `caniuse-lite` (CC-BY-4.0, a browserslist data package pulled in by Vite tooling, never shipped to users) | scan of `node_modules/*/package.json` |

Still to verify against official docs when the code is written (each is listed again in the final
report's "Unverified assumptions" if it cannot be confirmed): Hocuspocus `onAuthenticate`/`connection.readOnly`
semantics, `beforeHandleMessage`, debounce option names and shutdown behaviour, `openDirectConnection`,
how to serve plain HTTP `/health` beside the WebSocket server, and the y-tiptap JSON helpers.

## 5. Workspace layout decision

Tiptap/ProseMirror require **one** copy of `prosemirror-model` and friends in a process (two copies break
`instanceof` checks and keyed plugins). The repo therefore gains an **npm workspace root**
(`package.json`: `packages/editor-schema`, `frontend`, later `collab`), so there is one hoisted
`node_modules`. `client/` stays outside (it has no Tiptap dependency). The schema package is consumed
through a Vite/vitest alias to its source in the browser and through its built `dist/` in Node.

## 6. Checkpoints and what each proves

1. **Inspection, baseline, shared schema package** (this document + `packages/editor-schema`): extension
   list, adapter, `validateDocument`, hashes, golden fixtures (13 interchange fixtures each with an
   independently written expected Tiptap JSON), 91 package tests + a frontend test importing the same package.
2. Collaboration service + internal API + migration (tables, lease, authorize, load/store, lazy seeding,
   debounced persistence, validated snapshots, health, shutdown), tested with two real connections.
3. Frontend: Tiptap editor, provider lifecycle, toolbar, tables, links, images, sync status, switching;
   remove the old editor and save flow.
4. Access enforcement over time, limits, throttling, read-only enforcement, close codes.
5. Cleanup/integration: snapshot-driven asset references, templates through the seeding path,
   `migrate_documents_to_collab`, dead-code removal, deprecation notes.
6. AI-ready hooks, full browser tests, final report.
