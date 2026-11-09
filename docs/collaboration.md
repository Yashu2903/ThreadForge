# Collaborative documents: Tiptap + Yjs + Hocuspocus

> Status: this file grows with each checkpoint. Sections marked **(cp N)** are written when that
> checkpoint lands; nothing below describes behaviour that does not exist yet.

## 1. Words used here

* **ProseMirror**: the editing library underneath Tiptap. It owns a *schema* (which nodes and marks may
  exist and how they nest) and rejects anything outside it.
* **Tiptap**: a layer on ProseMirror made of *extensions*. Each extension adds nodes, marks, commands or plugins.
* **Yjs**: a CRDT library. A `Y.Doc` is a shared data structure; two people can change it at the same
  time and every copy converges to the same result with no central ordering and no version conflicts.
* **Hocuspocus**: a Node WebSocket server that speaks the Yjs sync protocol and calls our hooks
  (authenticate, load, store).
* **Interchange JSON**: the existing canonical document format (`documents.content`, templates, the
  Python validator, the future DOCX converter). **Tiptap JSON**: the same content in the editor schema.
  `adapt()` converts the first into the second.
* **Seed**: the one-time conversion of a legacy document's interchange JSON into a Yjs document.

## 2. Shared package `packages/editor-schema` (cp 1)

One npm workspace package, `@threadforge/editor-schema`, imported by the browser editor and by the
Node collaboration service, so both always use the same schema. Evidence: `frontend/test/editor-schema.test.ts`
imports it through the Vite alias; both sides compare `schemaFingerprint()` with the pinned
`packages/editor-schema/schema.fingerprint`. Changing any extension changes the fingerprint and fails
both test suites until the file is updated deliberately.

| Export | Purpose |
|---|---|
| `createExtensions(options)`, `getEditorSchema()` | the extension list and the ProseMirror schema built from it |
| `validateDocument(json, limits?)` | structure, limits, silent-drop detection; returns errors with paths |
| `adapt(interchange)` | interchange → Tiptap JSON; throws `AdaptError` with a path on anything unmappable |
| `canonicalize`, `canonicalStringify`, `contentHash`, `blockHash`, `blockHashes` | stable serialization and SHA-256 hashes |
| `extractText`, `normalizeWhitespace` | plain text of a document |
| `FONT_KEYS`, `DEFAULT_LIMITS`, `checkLink`, `normalizeColor` | allowlists and policies |

### 2.1 Which Tiptap extensions are free, and which were written

All `@tiptap/*` packages used are the open-source MIT packages at 3.33.0. **No `@tiptap-pro/*`, no
Tiptap Cloud, no hosted service.** Collaboration extensions are added in checkpoint 3.

| Capability | Source |
|---|---|
| doc, text, paragraph, heading (levels 1–3), bold, italic, underline, strike, hard break, bullet/ordered list | free: `@tiptap/starter-kit` (configured) |
| blockquote (paragraphs only) | free `@tiptap/extension-blockquote`, **content rule overridden** |
| list items (nesting ≤ 3 by validation) | free `@tiptap/extension-list`, **content rule overridden** |
| link (http/https/mailto) | free `@tiptap/extension-link`, **attributes reduced to `href`**, URL policy replaced |
| text style carrier | free `@tiptap/extension-text-style` |
| highlight | free `@tiptap/extension-highlight`, **colour normalized to #RRGGBB, default yellow** |
| superscript / subscript | free `@tiptap/extension-superscript`/`-subscript`, **made mutually exclusive** |
| text alignment | free `@tiptap/extension-text-align` |
| tables | free `@tiptap/extension-table` (+ prosemirror-tables), **cell content restricted, no merge commands exposed, merged cells rejected by validation** |
| stable block ids | free `@tiptap/extension-unique-id` |
| undo/redo | built-in history **disabled** (`undoRedo: false`); Yjs undo is added in checkpoint 3 |
| **ParagraphFormat** (line spacing, space before/after, indent) | **written by us** |
| **TextFormat** (font key from allowlist, size 8–72 pt, text colour) | **written by us** (attributes on the free `textStyle` mark) |
| **AssetImage** (inline image by `assetId`, no `src`) | **written by us** |
| Code, code block, horizontal rule, trailing node | disabled on purpose |

### 2.2 Interchange → Tiptap mapping (`adapt`)

| Interchange | Tiptap JSON |
|---|---|
| `{schema_version:1, blocks}` | `doc` (schema_version must be 1; no blocks → error) |
| `paragraph` `{id, attrs, children}` | `paragraph` `attrs.id` |
| `heading` `{level}` | `heading` `attrs.level` (level 1 omitted: canonical default) |
| `attrs.align` | `attrs.textAlign` |
| `attrs.line_spacing` | `attrs.lineSpacing` |
| `attrs.space_before_pt` / `space_after_pt` | `attrs.spaceBefore` / `attrs.spaceAfter` |
| `attrs.indent_level` | `attrs.indent` |
| `quote` | `blockquote` (children must be paragraphs) |
| `list {ordered}` / `list_item` | `orderedList`/`bulletList` / `listItem` (children: paragraphs and nested lists) |
| `table` / `table_row` | `table` / `tableRow` |
| `table_cell {header}` | `tableCell`, or `tableHeader` when `header: true` |
| `hard_break` | `hardBreak` |
| `image {asset_id, alt, width_px, height_px}` | `image {assetId, alt, width, height}`; **interchange image `id` is not kept** |
| text `marks.bold/italic/underline/strike` | marks `bold`, `italic`, `underline`, `strike` |
| `marks.script` | mark `superscript` or `subscript` |
| `marks.font`, `size_pt`, `color` | one `textStyle` mark `{fontKey, fontSizePt, color}` |
| `marks.highlight` | mark `highlight {color}` |
| `marks.link.href` | mark `link {href}` |
| empty text node | removed |
| any unknown field, type, or mark key | **`AdaptError` with the JSON path** (never dropped silently) |

Output is canonicalized: null and default attributes are omitted and marks are in schema order
(link, textStyle, bold, italic, strike, underline, highlight, superscript, subscript). The `id` of every
block, list item, row and cell is copied from the interchange document.

Golden fixtures: `shared/fixtures/valid/*.json` and `shared/fixtures/canonical/*` (interchange) each have an
expected output in `shared/fixtures/tiptap/` written by an independent Python port of this table.
`packages/editor-schema/test/fixtures.test.ts` checks, for all 13: adapter output equals the expected JSON,
`validateDocument` accepts it, and a ProseMirror load/serialize round trip is identical.

### 2.3 What `validateDocument` checks

1. a walk over the **input JSON** (unknown node/mark types, id format and uniqueness, values and ranges,
   link policy, table shape, list depth, text rules, limits);
2. `schema.nodeFromJSON(json).check()` (ProseMirror's own content rules);
3. **silent-drop detection**: ProseMirror ignores unknown *attributes* while loading. The canonical input is
   compared with the canonical re-serialization; any difference is `would_be_dropped` with a path.

Limits (`DEFAULT_LIMITS`): 1 MiB serialized, 100,000 code points, 5,000 blocks + list items, 60,000 nodes,
100 images, list depth 3, link 2,048 characters, alt text 500, tables 50 × 20, image side ≤ 10,000 px.
Exceeding a limit is an error with the offending path; nothing is truncated.

## 3. Dependency and licence table (cp 1)

Scanned from the installed tree (`node_modules/*/package.json`, hoisted by the npm workspace).

| Package | Version | Licence |
|---|---|---|
| `@tiptap/core`, `/pm`, `/starter-kit`, `/extension-*` (blockquote, highlight, link, list, subscript, superscript, table, text-align, text-style, unique-id) | 3.33.0 | MIT |
| `prosemirror-model` / `-state` / `-view` / `-transform` / `-tables` | 1.25.13 / 1.4.4 / 1.42.9 / 1.12.5 / 1.8.5 | MIT |
| `orderedmap`, `rope-sequence`, `linkifyjs`, `uuid` | 2.1.1, 1.3.4, 4.4.0, 14.0.3 | MIT |
| `vitest`, `typescript`, `@types/node` (dev only) | pinned in `packages/editor-schema/package.json` | MIT / Apache-2.0 |

Added by the collaboration service (checkpoint 2), all **MIT**:

| Package | Version | Role |
|---|---|---|
| `@hocuspocus/server`, `/common`, `/provider`, `/extension-throttle` | 4.8.0 | Yjs sync server (+ test client, + connection throttle in cp 4) |
| `yjs`, `y-protocols`, `lib0`, `isomorphic.js` | 13.6.33, 1.0.7, 0.2.119, 0.2.5 | the CRDT and its wire protocol |
| `@tiptap/y-tiptap` | 3.2.0 | ProseMirror ↔ Yjs conversion (Tiptap's maintained fork of y-prosemirror) |
| `crossws`, `async-mutex`, `kleur` | 0.4.17, 0.5.0, 4.1.5 | Hocuspocus' own dependencies |
| `ws`, `pg`, `tsx` (dev only) | 8.22.2, 8.23.1, 4.23.15 | test client sockets, test fixtures, `npm run dev` |

Whole install tree by licence: MIT (236), Apache-2.0 (8), ISC (9), BSD-3-Clause (2), BSD-2-Clause (2),
MIT-0 (1), 0BSD (1), plus our own three private workspace packages (no licence field; the repository's
`LICENSE` applies).

**Flagged: not MIT/BSD/Apache-style:** `caniuse-lite` 1.0.30001823 is **CC-BY-4.0** (a data package pulled in by
browserslist/Vite tooling; it is build-time data, attribution-only and is not shipped in our bundles).
No `@tiptap-pro/*`, Tiptap Cloud, Hocuspocus Cloud or other hosted service is used anywhere.

## 4. Architecture (cp 2)

```
 browser ──WebSocket  /collab (same origin, through the reverse proxy)──▶  collab/ (Node, Hocuspocus)
 browser ──HTTPS      /rooms/... (unchanged)──────────────────────────▶  FastAPI ◀── PostgreSQL
                                                                           ▲
 collab/ ──HTTP  /internal/collab/*  (shared secret + lease token, private network only)
```

* `collab/` holds **no durable state**. Everything it persists goes through `/internal/collab/*`
  (`backend/app/api/internal_collab.py` → `backend/app/services/collab.py`), the only code that writes
  `document_yjs_state`, `document_snapshots`, `collab_lease` and `documents.collab_state`.
* The reverse proxy must forward `/collab` (WebSocket upgrade, no path rewriting) to the service and
  must **not** route `/internal/*`. This repository does not ship a proxy yet; in development
  checkpoint 3 adds a Vite proxy for `/collab`. The service is not published to the host in Compose.
* Source of truth: a `legacy` document is seeded **once** from `documents.content` (interchange JSON →
  `adapt` → `validateDocument` → Yjs). After that `document_yjs_state` is the only writable content and
  `documents.content`, `content_version` and `content_hash` are never written again (tested).

### 4.1 States of a document (`documents.collab_state`)

| State | Meaning | Who sets it |
|---|---|---|
| `legacy` | not yet in Yjs; `documents.content` is the seed | default for every existing and new document |
| `ready` | `document_yjs_state` exists and is the only writable content | set in the **same transaction** as the seed insert |
| `needs_repair` | seed/load failed, or the live document stopped validating | the collab service (`/mark`); cleared by a live document becoming valid again, or by `restore_document` |

### 4.2 Request flows

**Open a document** (`collab/src/server.ts`): `onUpgrade` (path `/collab`, **exact** `Origin` match, process
cap) → client sends the document name `doc:<uuid>` → `onAuthenticate`: name format, Origin again,
`POST /internal/collab/authorize` with the raw `Cookie` header (FastAPI parses the session cookie and uses
the same `load_session` as HTTP) → role → `connectionConfig.readOnly = !can_edit` → per-user cap → context
`{userId, displayName, sessionId, role, canEdit, documentId, roomId}` (never the cookie) →
`onLoadDocument`: `GET state`; if present apply it, else **seed**: `GET seed` → build the Yjs document
*aside* → `PUT state` with `X-Insert-If-Absent` (database `ON CONFLICT DO NOTHING`; a loser gets 409
`already_seeded` and adopts the stored state instead) → only then apply to the live document → write a
`seed` snapshot. Concurrent loads of one document in one process are already collapsed by Hocuspocus
(`loadingDocuments`), and across processes by the database insert and the lease.

**Edit**: Yjs updates flow through the server to the other connections. A read-only connection's updates
are dropped by Hocuspocus (verified in `MessageReceiver`: sync step 2 and update messages are
acknowledged `false` and not applied), however the client behaves.

**Store** (`onStoreDocument`, debounce 2 s, at most 10 s): encode the full state, `PUT state` with the lease
token and a counter that must increase. Transient failure: retry (`COLLAB_STORE_RETRIES`, exponential
backoff), then keep the document in memory and try again after `COLLAB_STORE_RECOVERY_DELAY_MS`.
`stale_lease` → the lease is lost (see 4.4). `stale_counter` → resynchronize the counter and retry.
Stores take **only the document row lock**, never the room lock, and emit **no room events**.

**Snapshot** (see 4.5) and **close** (last connection leaves → store → final snapshot → unload).

### 4.3 Internal API (`/internal/collab`, secret `X-Internal-Secret`)

| Route | Purpose |
|---|---|
| `POST /lease/acquire`, `/renew`, `/release` | the single-instance lease |
| `POST /authorize` | cookie header + document id → identity, role, `can_edit`, `collab_state` (401 `session_invalid`, 403 `forbidden`; also 403 for a missing document) |
| `GET /documents/{id}/state` | raw Yjs bytes; metadata in `X-Collab-State`, `X-Update-Counter` (-1 = none), `X-Room-Id` |
| `PUT /documents/{id}/state` | store (lease + `X-Update-Counter`), or seed (`X-Insert-If-Absent: true`) |
| `GET /documents/{id}/seed` | `{format: interchange|tiptap, content}`; 409 `already_seeded` / `needs_repair` |
| `POST /documents/{id}/snapshot` | validated JSON snapshot + asset check + `document_asset_refs`, one transaction |
| `POST /documents/{id}/mark` | `needs_repair` ↔ `ready` (never `legacy`; never `ready` without Yjs state) |
| `POST /assets/check` | which asset ids are missing/expired in a room |

Gates: the proxy does not route `/internal`; the secret is compared with `hmac.compare_digest` and is
**required in production** (≥ 32 random characters; an unset secret makes every route answer 503, never
open); no CORS headers exist; the routes are hidden from OpenAPI; they never accept a browser session
(`test_every_internal_route_requires_the_shared_secret`). Nothing in this module logs a secret, cookie
or document content.

### 4.4 The lease (single instance, fenced)

One row in `collab_lease`. `acquire` succeeds if the lease is free or expired and **increments**
`fencing_token`. The service renews every 10 s (TTL 30 s). Every write sends the token, and the API
checks it inside the write transaction with `SELECT … FOR SHARE` on the lease row, which conflicts with a
new acquisition: a paused old holder that wakes up cannot slip a write in, and after takeover every one
of its writes fails with 409 `stale_lease`. On `stale_lease`, or if renewal cannot reach the API for
`COLLAB_LEASE_GRACE_MS` (25 s, shorter than the TTL), the service reports the lease lost (default action:
exit; `/health` turns 503) and does no more writes. After a crash the next instance waits for the TTL
(≤ 30 s); a graceful shutdown releases the lease immediately.

### 4.5 Snapshots and validation

A snapshot is the document as plain editor-schema JSON, **validated** by `validateDocument` (the same
package function the browser uses). `seed`: when seeding. `periodic`: every 30 s if the document changed,
built from the **exact bytes of the last stored state** so its `yjs_update_counter` is correct. `final`:
when the last connection leaves (after the final store). The API keeps the newest 10 plus the newest seed
and skips an insert whose hash equals the latest snapshot's.

* **Valid** → stored; `document_asset_refs` is made equal to the images in the snapshot (new assets become
  `attached`, dropped ones get `unreferenced_since`); a live `needs_repair` document returns to `ready`.
* **Invalid** (schema/limits, or an asset missing from the room) → **raw Yjs is still stored**, the last
  valid snapshot is kept, `collab_state = needs_repair`, new connections are refused with reason
  `needs_repair`. Exception: *duplicate block ids only* are tolerated for one tick (two clients can create
  the same id at once; Tiptap's UniqueID repairs it).
* **Operator restore:** stop the service, then
  `python -m app.commands.restore_document --document <uuid> --backup-dir <dir>` (backs up the raw Yjs
  state first, refuses while a lease is live, copies the newest snapshot to a `restore` snapshot and sets
  the document back to `legacy`; it is re-seeded from that snapshot on next open).

### 4.6 Crash-loss window

Edits are in memory until the debounced store: a hard crash of the Node process loses at most
**~2 s after the last keystroke, 10 s of continuous typing** (`debounce`/`maxDebounce`). A graceful stop
loses nothing (stores first). "Synced" in the UI will mean *the server received it*, not *PostgreSQL has
it*; the indicator wording (cp 3) says so.

### 4.7 Close codes and rejection reasons

Authentication failures are Hocuspocus `permission-denied` messages with a `reason` (the socket stays
open until the unauthenticated timeout): `bad_document`, `origin`, `session_invalid` (401),
`forbidden` (403: not a member / no such document), `needs_repair`, `too_many_connections`,
`unavailable` (internal API or database down). WebSocket close codes used: **1001** server restarting,
**1009** message larger than `COLLAB_MAX_MESSAGE_BYTES` (enforced by `ws` before parsing), **4429** over the
per-user cap after authentication raced. Runtime access revocation codes (**4401**, **4403**, **4408**,
**1013**) and the rate-limit code arrive in checkpoint 4.

### 4.8 Settings

| Where | Setting | Default |
|---|---|---|
| API | `INTERNAL_API_SECRET` (required in production) | unset = routes 503 |
| API | `COLLAB_LEASE_TTL_SECONDS`, `COLLAB_MAX_STATE_BYTES`, `COLLAB_SNAPSHOTS_KEPT` | 30, 8 MiB, 10 |
| service | `COLLAB_API_BASE_URL`, `INTERNAL_API_SECRET`, `COLLAB_ALLOWED_ORIGINS` (or `TRUSTED_ORIGINS`) | required |
| service | `COLLAB_PORT`, `COLLAB_ADDRESS`, `COLLAB_PATH` | 4010, 127.0.0.1, `/collab` |
| service | `COLLAB_DEBOUNCE_MS`, `COLLAB_MAX_DEBOUNCE_MS`, `COLLAB_SNAPSHOT_INTERVAL_MS` | 2000, 10000, 30000 |
| service | `COLLAB_LEASE_RENEW_MS`, `COLLAB_LEASE_GRACE_MS` | 10000, 25000 |
| service | `COLLAB_MAX_MESSAGE_BYTES`, `COLLAB_MAX_STATE_BYTES` | 1 MiB, 8 MiB |
| service | `COLLAB_MAX_CONNECTIONS_PER_USER`, `COLLAB_MAX_CONNECTIONS_TOTAL` | 10, 500 |
| service | `COLLAB_STORE_RETRIES`, `COLLAB_STORE_RETRY_DELAY_MS`, `COLLAB_STORE_RECOVERY_DELAY_MS` | 3, 500, 5000 |
| service | `COLLAB_LOG_LEVEL` | info |

### 4.9 Verified against the Hocuspocus 4.8.0 source (not memory)

`onAuthenticate` receives `requestHeaders`, a `Headers` object including `cookie` and `origin`;
`connectionConfig.readOnly` makes the server ignore that connection's updates; `Connection.readOnly` is a
mutable field (runtime downgrade is possible); a throw from `onAuthenticate`/`onLoadDocument` becomes a
`permission-denied` message carrying `error.reason`; `onStoreDocument` errors are only logged by Hocuspocus
(the document stays in memory) so retrying is our job; `debounce`/`maxDebounce`/`unloadImmediately`
options exist with the names used here; `websocketOptions.maxPayload` reaches `ws`; the per-document
`CLOSE` message carries only a reason string, so application close **codes** are sent by closing the
underlying socket; `HocuspocusProvider` exposes `unsyncedChanges`, `authorizedScope` and `onAuthenticationFailed`.

### 4.10 Known limits (cp 2)

* Awareness (cursors, names) is client-asserted and cosmetic; checkpoint 3 resolves names from the member list.
* A denied connection keeps its socket until the unauthenticated timeout (60 s); it still counts toward the per-process cap meanwhile.
* The strict per-user cap is enforced after the connection exists (authentication can race); the excess connection is closed with 4429.
* Until checkpoint 4 there is no message-rate limit, no connection throttle and no live re-check of access after connecting.
* A document at the 8 MiB state limit becomes read-only for everyone; its last oversized edit is not stored.

## 5. The browser editor (cp 3)

### 5.1 What replaced what

| Before | Now |
|---|---|
| custom editor (`frontend/src/editor/*`), `Playground` | Tiptap 3.33.0 (`@tiptap/react`) with the shared schema (`createExtensions`), `Collaboration` (field `default`) and `CollaborationCaret` |
| `PUT .../content`, `expected_content_version`, autosave, 409 "conflict" UI, "Load latest" | **removed** (route, service `save_content`, schemas, tests). There is nothing to save and nothing to conflict: edits merge (Yjs) and stream to the server |
| `GET document` returned the JSON content | returns metadata only: `collab_state`, `can_edit`, `collab_path` (`/collab`), `document_name` (`doc:<id>`); **no content**, no `content_version`/`content_hash` |
| `DocumentRead` (viewers, mobile) | the same editor with `editable=false`; viewers get a read-only socket and see edits live |
| custom undo/redo | Yjs `UndoManager` through the Collaboration extension: undo reverts only YOUR changes |
| `document_content_updated` per save | nothing emits it; the client ignores it (type kept in the DB CHECK) |

### 5.2 Files (reading order)

1. `frontend/src/collab/status.ts` - the pure function that turns raw provider state into the indicator wording; close codes.
2. `frontend/src/collab/session.ts` - `CollabSession`: one `Y.Doc` + one `HocuspocusProvider` per open document.
3. `frontend/src/collab/identity.ts` - caret names from the member list, colours from ids.
4. `frontend/src/editor/TiptapEditor.tsx` - the editor component (paste/drop guards, key handling).
5. `frontend/src/editor/formatState.ts`, `commands.ts`, `Toolbar.tsx` - the toolbar: tri-state reading, one Tiptap command per control.
6. `frontend/src/DocumentEditor.tsx`, `RoomView.tsx` - lifecycle, sync indicator, switching rule.
7. `backend/app/api/documents.py` (`GET document`), `services/documents.py` (metadata only).

### 5.3 Provider lifecycle

`DocumentEditor` creates a `CollabSession` in an effect and destroys it in the cleanup, keyed by document id.
Opening another document, leaving the room, an access-lost screen, or unmounting therefore always closes the
socket and destroys the `Y.Doc` (React StrictMode's mount-unmount-mount included). The connection is same origin
(`ws(s)://<host>/collab`), authenticated by the session cookie the browser already sends; the provider's `token`
is the fixed non-secret marker `"cookie"` because the protocol only authenticates when a token is present.
There is **no offline persistence** (no `y-indexeddb`): unsynced changes live in the tab only.

`editable` follows `can_edit` (from the document API) AND the room client's current role AND the server's
granted scope AND not-access-lost AND not-mobile. Losing the role makes the editor read-only without
replacing it; the text stays.

### 5.4 The sync indicator (exact wording)

| Indicator | When | What it means |
|---|---|---|
| **Synced** | connected, first sync done, 0 unacknowledged local changes | the SERVER has received everything you typed. It is written to PostgreSQL within a few seconds (debounced); a hard server crash in that window can lose those seconds |
| **Syncing…** | connected but first sync open, or changes still on their way | not yet safe |
| **Offline (n unsynced changes)** | no connection | edits stay in this tab and are sent when the connection returns; closing the tab loses them |
| **Read-only** | viewer or read-only scope | live, cannot change |
| **Access lost** | server denied/ended access (`session_invalid`, `forbidden`, `needs_repair`, close 4401/4403) | the text stays on the page for copying; no reconnect attempts |
| Connecting… | the very first connect | |

Temporary denials (`unavailable`, `too_many_connections`) retry the same `Y.Doc` after 3-5 s, so local edits
are kept and sent once access is granted. `beforeunload` warns while any change is unacknowledged.

### 5.5 Switching documents

Opening a document yourself while changes are unacknowledged asks for confirmation. When the owner makes
another document active:
* nothing unacknowledged: the tab follows by itself;
* unacknowledged changes: the tab stays and shows "The room switched to a different document. Your last
  changes are not synced yet" with **Stay here** and **Switch anyway and lose those changes**; once the
  server catches up the pending switch happens by itself (unless you chose Stay).

### 5.6 Names, identity, accessibility

Each collaborator announces only `{id}`; the screen shows the display name from the room's AUTHORITATIVE member
list (`Someone` for an unknown id) and a colour derived from the id; a client cannot choose either. Caret labels
are `aria-hidden` so they are not read as part of a sentence. The editor is `role="textbox"`, `aria-multiline`,
`aria-readonly` follows `editable`. The toolbar is `role="toolbar"` with one tab stop (roving tabindex), arrow
keys/Home/End move inside it, toggles use `aria-pressed` (`"mixed"` for partly formatted selections), unavailable
controls use `aria-disabled`; the link form is a popover that returns focus to the text; Escape closes it.
Shortcuts: Ctrl/Cmd+B/I/U, +Shift+S (strike), +K (link), +Z/+Shift+Z (undo/redo). Tab inside a list nests,
Shift+Tab lifts, Tab in a table moves to the next cell; otherwise Tab leaves the editor (no keyboard trap).

### 5.7 What cannot reach the document from the browser

* images: only through **Insert image**, which uploads to the existing asset endpoint and inserts `assetId`.
  Pasted/dropped files are refused with an explanation; pasted HTML has every `<img>` removed unless it is a
  copy of an image already in THIS document (so a forged `data-asset-id` of another room cannot reach a
  snapshot and break validation); `data:` and remote URLs never parse (the image node has no `src`).
* paste goes through the shared ProseMirror schema (anything the schema does not know is dropped); links are
  checked with the same policy as the server (`http`, `https`, `mailto`).
* merge/split table cells are not exposed; merged cells are rejected by the shared validator anyway.

### 5.8 Dev and deployment

Dev: run the API (`uvicorn`), the collaboration service (`npm run build` once at the repo root, then
`node collab/dist/main.js` with `COLLAB_API_BASE_URL`, `INTERNAL_API_SECRET`,
`COLLAB_ALLOWED_ORIGINS=http://localhost:5173`) and Vite. Vite proxies `/collab` (WebSocket) to
`THREADFORGE_COLLAB` (default `http://127.0.0.1:4010`), forwarding the browser's Origin unchanged because the
service compares it exactly. In production the reverse proxy must do the same for `/collab` and must NOT expose
`/internal`. `COLLAB_PUBLIC_PATH` (API setting, default `/collab`) is the path the document API tells the browser
to connect to.

**CSP.** The repository has no Content-Security-Policy today (the SPA is served without one), so there was none to
change. A deployment that adds one needs `connect-src 'self'` plus the same-origin WebSocket scheme
(`wss://<app host>`); nothing else is required by the editor (no third-party origins, no `unsafe-eval`).

### 5.9 Tests (cp 3)

* Browser (Playwright, real API + PostgreSQL + collaboration service + Vite): two users typing, read-only viewer,
  caret labels from the member list, persistence across a reload (the text comes from the database), toolbar
  (bold, heading, list, table, valid and refused links, undo), own-changes-only undo, image upload + alt text +
  paste filtering (Chromium), offline indicator and delivery on reconnect (a WebSocket proxy cuts the
  connection, identical in every engine), mobile view-only, the document-switching rules incl. `beforeunload`.
  Run in Chromium and Firefox.
* Unit (vitest): indicator wording, terminal vs temporary denials, identity (no HTML from a name), paste
  sanitising, document store (metadata only, content events ignored), dialogs.
* Backend: `GET document` shape, ETag, the removed route answers 404 for everyone, permission matrix.

### 5.10 Known limits (cp 3)

* Undoing the insertion of a table (or list) removes it together with text a collaborator typed inside it - the
  normal Yjs semantic for structural undo; undoing plain typing never removes another person's text.
* Firefox: the paste-filter browser test is skipped (synthetic paste events carry no clipboard data there); the
  sanitising function is unit-tested and the same code runs in every engine.
* Remote caret rendering is cosmetic and relies on awareness, which a modified client could spam; the service's
  message-rate limit (checkpoint 4) bounds that.
* Access enforcement over time (revoking a socket while it is open) is checkpoint 4. Until then a removed member
  loses the room screen at once (the Part 5 event), which unmounts the editor and closes the socket.

## 6. Later sections

Live access enforcement and limits (cp 4), import/migration commands and cleanup (cp 5) and the AI hooks (cp 6)
are written with the checkpoints that build them.
