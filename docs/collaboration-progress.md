# Collaboration progress

One section per checkpoint, one commit each. Plan: `docs/collaboration-migration-plan.md`. Design: `docs/collaboration.md`.

## Checkpoint 1: inspection, baseline, shared editor-schema package, adapter, validator, fixtures

**Done**
* Inspection report, baseline numbers and Keep/Replace/New lists: `docs/collaboration-migration-plan.md`.
* npm workspace root (`package.json`) so there is exactly one copy of Tiptap/ProseMirror/Yjs; `frontend/package-lock.json`
  replaced by the root `package-lock.json`.
* `packages/editor-schema`: extensions (3 written, others configured), schema + fingerprint, `validateDocument`
  (with silent-drop detection), `adapt`, canonicalization, hashes, text extraction.
* 13 expected Tiptap JSON fixtures (`shared/fixtures/tiptap/`) from an independent port of the mapping.
* Tests: package vitest 91 passed; `frontend/test/editor-schema.test.ts` 3 passed (browser side imports the same package).

**Existing suites after this checkpoint:** frontend vitest 189 + 3 new; no backend or client code changed.

**Blocked: nothing to migrate** (found by inspection, see plan section 0): import jobs, DOCX converter,
Google Drive were never built. Requirements about them are not claimed. The seeding path accepts interchange JSON so
they can plug in later.

**Verified against docs / registry:** Tiptap 3.33.0 and `undoRedo` StarterKit option, unique-id extension
and options, `@tiptap/extension-collaboration-caret` name, Hocuspocus 4.8.0 package names, licences of the install tree.

**Not yet verified at cp 1 (verified in cp 2 against the 4.8.0 source, see docs/collaboration.md 4.9):** Hocuspocus hook
semantics, read-only connections, debounce options, HTTP `/health` beside the WebSocket (`onRequest` hook), y-tiptap JSON helpers
(`prosemirrorJSONToYDoc`, `yDocToProsemirrorJSON`), the provider's `unsyncedChanges`. **Still unverified:** `beforeHandleMessage`
as a rate-limit point, `openDirectConnection` (cp 4/6), the CollaborationCaret API (cp 3).

## Checkpoint 2: collaboration service and internal API

**Done**
* Migration `0006_collaboration` (expand only): `documents.collab_state` (CHECK legacy/ready/needs_repair, default
  `legacy`), `document_yjs_state`, `document_snapshots`, `collab_lease` (single row). Nothing dropped. The downgrade
  refuses while any document keeps its content only in Yjs.
* `backend/app/services/collab.py` + `backend/app/api/internal_collab.py` (`/internal/collab/*`): lease acquire/renew/release
  (fencing token, `FOR SHARE` fence inside every write), authorize, load/store state (counter must rise, 8 MiB cap, seed =
  insert-if-absent that also sets `ready`), seed source, mark, validated snapshot (+asset check +`document_asset_refs` in one
  transaction, prune to 10 + newest seed, dedupe), asset check. Shared-secret gate, 503 when unconfigured, production requires a
  strong secret. Document row locks only; no room lock, no room events; `documents.content` never written.
* `backend/app/commands/restore_document.py`: operator restore (backup first, refuses while a lease is live, re-seeds from a snapshot).
* `collab/` (Node, Hocuspocus 4.8.0): lease keeper, authenticate/authorize, read-only from role, lazy seeding, debounced fenced
  stores with retry/recovery, periodic/final snapshots with validation, `needs_repair` handling, state-size and message-size
  limits, per-user/per-process caps, `/health`, graceful shutdown (1001, store, final snapshot, release lease), structured
  logs without content. `collab/Dockerfile` (image built and smoke-tested) and a Compose service behind the `collab` profile,
  not published to the host.
* Docs: `docs/collaboration.md` sections 3–5.

**Tests (all against real PostgreSQL, real FastAPI over HTTP, real WebSocket clients)**
* backend `tests/test_collab_internal.py`: 30 tests (secret, lease and fencing, authorize matrix, seeding once under
  6-way concurrency, store/counter rules, size cap, DB constraints, snapshots/prune/dedupe, asset lifecycle and cross-room
  refusal, needs_repair, restore service and command). `test_csrf_dependency.py` now also proves every `/internal` route
  requires the secret and refuses browser sessions.
* `collab/test` 28 tests: convergence, late joiner, exactly-once seeding with 6 simultaneous clients, `documents.content`
  untouched, restart persistence with a continuing counter, viewer edits from a modified client ignored, Origin/cookie/
  non-member/bad-name/path refusals, no cookie in context, caps, health, graceful shutdown + lease release, store retry
  and long-outage recovery, load-time outage not marking the document, lease exclusivity and fencing (stale writer cannot write),
  renewal, periodic/final snapshots, invalid structure → `needs_repair` with raw Yjs kept and last valid snapshot kept,
  self-healing, duplicate-id tolerance, asset references and missing-asset refusal, seed failure, corrupt state,
  restored-snapshot seeding, message-size close (1009), state-size read-only.

**Changed existing tests and why:** `test_csrf_dependency.py` (the audit now excludes `/internal/*` from the *browser* rules and
adds the secret rule for them); `tests/db_utils.py` truncates the three new tables; `tests/factories.py` supplies a test
internal secret; `scripts/e2e_prepare.py` also accepts `threadforge_collab_e2e`.

**Deferred to later checkpoints (not claimed here):** rate limit and connection throttle, live access re-check and close codes
4401/4403/4408/1013 (cp 4); the editor and the Vite `/collab` proxy (cp 3); `migrate_documents_to_collab` and cleanup guard (cp 5);
AI hooks (cp 6).

**Checkpoint 2 results (final run, sequential batches because one full run was reaped for low memory):**
backend 1284 passed in 4 batches (baseline 1251; +33 new), 0 failed; `collab` vitest 28 passed; schema package 91 passed;
ruff check and format clean; `tsc --noEmit` clean. Not re-run at this checkpoint because nothing they cover changed:
`client/` vitest (177) and Playwright (69 + 2 skipped).

## Checkpoint 3: the Tiptap editor in the browser

**Done**
* Frontend: `@tiptap/react` editor on the shared schema with `Collaboration` + `CollaborationCaret`; one
  `CollabSession` (Y.Doc + HocuspocusProvider) per open document, destroyed on switch/unmount; sync indicator with
  exact wording (Synced / Syncing / Offline (n unsynced changes) / Read-only / Access lost); `beforeunload`
  warning; denial handling (terminal vs retry); document-switching rule with Stay / Switch anyway; names and
  colours from the authoritative member list; mobile and viewers are read-only live; toolbar rebuilt on Tiptap
  commands (roving tabindex, `aria-pressed` incl. mixed, `aria-disabled`, link and alt-text popovers, shortcuts,
  tables without merge/split); image upload through the asset endpoint; paste/drop guards.
* Backend: `GET document` is metadata only (`collab_state`, `can_edit`, `collab_path`, `document_name`);
  `PUT .../content`, `save_content`, `expected_content_version`, the save body-limit rule and per-save
  `document_content_updated` emission are removed; `COLLAB_PUBLIC_PATH` setting. DB objects kept (deprecated).
  Asset tests that used saving now drive `sync_asset_refs` (the function the validated snapshot uses).
* Removed code: `frontend/src/editor/{doc,dom,editor,EditorSurface,format,history,images,inline,segment,stats,structure}`,
  `documents/session.ts`, `DocumentRead.tsx`, `Playground.tsx`, their 7 test files and 4 browser specs, and
  `backend/tests/test_documents_save.py`.
* Dev/e2e wiring: Vite `/collab` WebSocket proxy; the Playwright setup starts the real collaboration service after
  the database is prepared.
* Found and fixed a latent AI-checkpoint-1 defect: `.env.example` had empty numeric AI values, which the existing
  `test_env_example_is_valid_for_development_and_unsafe_for_production` rejects (I had appended the section after
  running that batch). Unset numeric settings are now commented out.

**Verified (automated)**
* Browser (Playwright, real API + PostgreSQL + collaboration service + Vite): 21 passed, 1 skipped
  (the Firefox paste test: synthetic paste events carry no clipboard data there), Chromium and Firefox.
* frontend vitest 38 passed (collab status/identity 9, paste 2, store 11, dialogs 12, schema 3, others);
  `tsc --noEmit` clean; `vite build` succeeds.
* backend 1351 passed in sequential batches (271 + 657 + 423), ruff clean. `client/` vitest 177 passed,
  schema package 91 passed.
* Not re-run: the collab service vitest (28; the service was not changed in this checkpoint).

**Changed existing tests and why**: `test_documents_api.py` (shape without content, new ETag, removed route is 404),
`test_assets.py` (references via `sync_asset_refs`, the save-quota test is gone with the quota), `test_room_events.py`
(`save_content` removed from the audit; the deprecated event type has no producer), frontend `store.test.ts`
(metadata only, content events ignored), `room-layout.spec.ts` (viewer reads in the editor).

**Not verified / limits**: see `docs/collaboration.md` 5.10. No CSP exists in the repository to change (documented
what a deployment needs). Screen-reader behaviour was reasoned from ARIA roles, not tested with a screen reader.
Access enforcement while the socket is open (revoking a member's document socket) is checkpoint 4.
