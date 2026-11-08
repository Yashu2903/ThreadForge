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
