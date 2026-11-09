# Collaborative editing: final report

Direction (from the owner): replace the custom editor and versioned JSON saves with **Tiptap + Yjs + Hocuspocus**,
self-hosted, free and open source only. Six checkpoints, one commit each:

| # | Commit subject | What it delivered |
|---|---|---|
| 1 | shared editor-schema package, adapter, validator, golden fixtures | one schema for browser and service; 13 golden fixtures; 91 tests |
| 2 | collaboration service and internal API | Hocuspocus service, fenced lease, Yjs persistence, validated snapshots, seeding, restore command |
| 3 | Tiptap editor in the browser | editor, provider lifecycle, sync indicator, toolbar, switching rule; document API without content |
| 4 | live access enforcement, rate limit, throttle, close codes | re-check of open sockets, runtime read-only, 1013 grace, 4430, throttle |
| 5 | migrate_documents_to_collab, cleanup guard, templates, dead code | operator migration, asset guard, README rewrite, deprecation list |
| 6 | AI-ready hooks, integration, this report | read and change-a-block hooks, end-to-end browser test, final numbers |

Design, flows and failure modes: `docs/collaboration.md`. Per-checkpoint log: `docs/collaboration-progress.md`.

## 1. Files changed (by area)

* **`packages/editor-schema/`** (new): extensions (3 written: ParagraphFormat, TextFormat, AssetImage), schema +
  fingerprint, `validateDocument`, `adapt`, canonical JSON, hashes, text, tests.
* **`collab/`** (new, Node): `server.ts` (hooks), `access.ts`, `ai-hooks.ts`, `api.ts`, `lease.ts`, `documents.ts`,
  `config.ts`, `main.ts`, `Dockerfile`, 6 test files (64 tests).
* **`backend/`**: migration `0006` (expand only), `services/collab.py`, `services/permissions.py` (shared
  `decide_write`), `api/internal_collab.py` (lease, authorize, can-write, access check, state, seed, mark, snapshot,
  assets), `commands/restore_document.py`, `commands/migrate_documents_to_collab.py`, `commands/cleanup_documents.py`
  (guard), `api/documents.py` and `services/documents.py` (metadata only), config, tests.
* **`frontend/`**: `collab/` (status, session, identity), `editor/` (TiptapEditor, Toolbar, formatState, commands, fonts),
  `DocumentEditor.tsx`, `RoomView.tsx`, `documents/store.ts`, Playwright setup that starts the real service.
* **Repo**: root npm workspace (`package.json`), `compose.yaml` (`collab` service behind a profile), `.env.example`, README,
  `docs/`.

## 2. Removed code

Custom editor and toolbar (`frontend/src/editor/{doc,dom,editor,EditorSurface,format,history,images,inline,segment,stats,structure}.ts*`),
the save/autosave/conflict state machine (`documents/session.ts`), `DocumentRead.tsx`, `Playground.tsx`,
`PUT .../content` with `save_content`, `SaveContentRequest/Out`, `document_version_conflict`, the save rate limit
and body-limit rule (`DOCUMENT_WRITES_PER_MINUTE`, `DOCUMENT_SAVE_BODY_LIMIT_BYTES`), per-save
`document_content_updated` emission, the TypeScript twin validator `client/src/document-schema.ts`, and the tests
and browser specs of all of them.

## 3. Deprecated database objects (kept, nothing dropped)

`documents.content` (now read-only seed), `documents.content_version`, `content_hash`, `content_updated_at`,
table `document_write_buckets`, event type `document_content_updated` in the `room_events.type` CHECK. Migration
`0006` only adds (`documents.collab_state`, `document_yjs_state`, `document_snapshots`, `collab_lease`); its
downgrade refuses while any document keeps its content only in Yjs. Details in README Part 6.

## 4. Test results against the baseline

Baseline = the repository before any collaboration change (`docs/collaboration-migration-plan.md` section 1).

| Suite | Baseline | Now | Why it differs |
|---|---|---|---|
| backend pytest (real PostgreSQL, Redis) | 1251 passed | **1359 passed** (279 + 657 + 423, sequential batches) | + service/API/permission tests, - the save tests (the route is gone). Includes 106 tests of the separate AI work |
| collaboration service vitest (real FastAPI + PostgreSQL + Redis + WebSockets) | n/a | **64 passed** | new |
| shared schema package vitest | n/a | **91 passed** | new |
| frontend vitest | 189 | **39 passed** | the custom editor/session tests (about 160) were removed with the code; new: indicator, identity, paste, store, dialogs |
| `client/` vitest | 177 | **69 passed** | the 108 tests of the removed TypeScript validator are gone; the room client is unchanged |
| Playwright (real API + service + Vite; Chromium + Firefox) | 69 passed, 2 skipped | **29 passed, 1 skipped** | the old editor/saving specs were replaced by `editor-collab.spec.ts` (15 scenarios x 2 engines, one Firefox skip) |
| lint / types / builds | clean | `ruff check` + `ruff format --check`, `tsc --noEmit` (frontend, collab), `vite build`, `alembic check`: all clean | |

Fewer tests than before on the frontend side is not lost coverage: the removed tests covered code that no longer
exists. What replaced them is end-to-end (two real browsers, the real service and database).

One latent defect found during the work and fixed: the AI checkpoint-1 `.env.example` had empty numeric values that
an existing config test rejects (it had been appended after that batch ran).

## 5. Verified against sources and tests vs assumed

**Verified** (source reading of Hocuspocus 4.8.0 and tests): `onAuthenticate` request headers and `connectionConfig.readOnly`;
`Connection.readOnly` is mutable and a read-only connection's updates are dropped (runtime downgrade test);
`beforeHandleMessage` exists and a throw there drops the message (rate-limit test); `openDirectConnection` and its
`transact` origin (migration and hook tests); the throttle extension keys on `X-Real-IP`/`X-Forwarded-For`
(spoofing test); `unsyncedChanges`/`authorizedScope`/`onStateless` on the provider (browser tests); Tiptap
`undoRedo: false`, `UniqueID` with `isChangeOrigin`, `CollaborationCaret` `render`, `prosemirrorJSONToYXmlFragment`
for nested replacement (browser and hook tests); licences of the whole install tree.

### Unverified assumptions (still open)

1. **Behind a real reverse proxy** (TLS termination, WebSocket upgrade headers, `X-Real-IP` set by the proxy) nothing
   was run; the throttle's proxy mode is tested with headers only.
2. **Docker image**: `collab/Dockerfile` was built and smoke-tested at checkpoint 2, but not rebuilt after the `redis`
   dependency was added at checkpoint 4. `compose.yaml` was only validated (`docker compose config`).
3. **Redis with a password or TLS** for the room hints: the client accepts such URLs, only plain local Redis was exercised.
4. **Screen readers**: ARIA roles/states and focus behaviour follow the patterns, but nothing was tested with a screen reader or on a real phone.
5. **Scale and soak**: tested with a few dozen connections on one machine, not thousands, and not for hours.
6. **Linux process signals** for graceful shutdown (development ran on Windows).
7. **Hard-crash loss window**: documented as "a few seconds" from the debounce settings; not measured by killing the process mid-edit.
8. **Racing hook calls**: two AI-hook applications racing each other or a collaborator at the same instant are safe by construction
   (one synchronous transaction with a re-check) but have no stress test.
9. **Firefox paste filter** is unit-tested but its browser-level test is skipped there (synthetic paste events carry no clipboard data).
10. `generateUniqueIds` (server-side id repair) is **not used**; duplicate ids from two clients are tolerated for one snapshot tick and
    otherwise repaired by the editor's UniqueID plugin. Not stress-tested with adversarial duplicate ids.

## 6. Per-document migration results

There is no production or development content to migrate in this repository, so no real migration was run against real
documents. What exists is the automated evidence from `collab/test/migrate.test.ts` (the real command, real API and
database): a dry run reports `would_migrate` for a valid document and `invalid  adapt_failed ...` for a broken one and
changes nothing; a real run migrates the valid one (`ready`, Yjs state, seed snapshot, `documents.content` byte-identical)
and leaves the broken one `legacy` (not `needs_repair`); a second run says `nothing to do`; `--document` limits it;
`needs_repair` documents are skipped; a wrong secret and an unreachable service are reported (exit 1 and 2). Run this
command with `--dry-run` first on any real deployment and keep its output.

## 7. Blocked items (never built in this repository)

Import jobs, the DOCX converter and Google Drive were listed under "keep" in the brief but do not exist (they belonged to
the old plan that this change replaced). Requirements that refer to them (import isolation/publication, Drive) are **not claimed**.
The seeding path accepts the interchange JSON, so an importer can feed it later.

## 8. Remaining work

* Run `migrate_documents_to_collab --dry-run` and then the real run on any environment that holds legacy documents.
* Rebuild and run the `collab` image behind the real reverse proxy; confirm `/collab` forwards the Origin header and `/internal` is not exposed.
* A later migration to drop the deprecated columns/table once every deployment has migrated.
* Optional: runtime role UPGRADE (a promoted viewer currently reloads the page); comments, history, export (explicitly out of scope).
* AI features that call these hooks (outside this change; see `docs/ai.md`).
