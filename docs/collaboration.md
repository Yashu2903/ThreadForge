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

Added by the browser editor (checkpoint 3) and the access/limits work (checkpoint 4), all **MIT**:

| Package | Version | Role |
|---|---|---|
| `@tiptap/react`, `@tiptap/extension-collaboration`, `@tiptap/extension-collaboration-caret` | 3.33.0 | the React editor, the Yjs binding, remote carets |
| `@hocuspocus/provider` | 4.8.0 | the browser's connection to the service |
| `redis` | 6.3.1 | the collaboration service's subscription to room hints |

Whole install tree by licence (final scan): MIT (253), Apache-2.0 (8), ISC (9), BSD-3-Clause (2), BSD-2-Clause (2),
MIT-0 (1), 0BSD (1), CC-BY-4.0 (1, flagged below), plus our own three private workspace packages (no licence field; the
repository's `LICENSE` applies).

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
**1009** message larger than `COLLAB_MAX_MESSAGE_BYTES` (enforced by `ws` before parsing), and the codes in the
table in section 6.2 (runtime access, rate limit, per-user cap).

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
| service | `COLLAB_ACCESS_CHECK_MS`, `COLLAB_ACCESS_GRACE_MS` | 15000, 30000 |
| service | `COLLAB_REDIS_URL` (or `REDIS_URL`), `COLLAB_REDIS_PREFIX` (or `REDIS_CHANNEL_PREFIX`) | empty (timer only), `threadforge` |
| service | `COLLAB_MESSAGES_PER_SECOND` | 100 |
| service | `COLLAB_CONNECTS_PER_MINUTE`, `COLLAB_BAN_MINUTES`, `COLLAB_TRUST_PROXY_HEADERS` | 60, 5, false |
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

* Awareness (cursors, names) is client-asserted and cosmetic; the editor resolves names from the member list (section 5.6).
* A denied connection keeps its socket until the unauthenticated timeout (60 s); it still counts toward the per-process cap meanwhile.
* The strict per-user cap is enforced after the connection exists (authentication can race); the excess connection is closed with 4429.
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

## 6. Access over time and limits (cp 4)

### 6.1 Why a check is needed

A WebSocket is authenticated once, when it opens. Without a re-check a removed member, a signed-out user or a
demoted editor would keep a working socket for as long as the tab stays open. `collab/src/access.ts`
(`AccessMonitor`) closes that gap.

```
every COLLAB_ACCESS_CHECK_MS (15 s)  ┐
Redis hint {prefix}:room:{room_id}   ├──► check the affected rooms ──► POST /internal/collab/access/check
 (at most 1 per room per second)     ┘            one request, two SQL statements PER ROOM
                                                        │
        session_invalid ─► close 4401     not_member / document_gone ─► close 4403
        ok, role now viewer ─► connection.readOnly = true + stateless {"type":"read_only"}
        API/DB failing ─► serve for COLLAB_ACCESS_GRACE_MS (30 s), then close 1013
```

* **Batching.** Connections are grouped by room; the API answers for all of a room's connections with one
  statement over `sessions` LEFT JOIN the room's active `memberships` and one over `documents`
  (`services/collab.check_access_batch`). 100 connections cost the same two statements as 1 (tested).
* **Redis is only a hint.** The outbox dispatcher already publishes a hint to `{prefix}:room:{room_id}` for every
  committed room event (member removed, role changed...). The service subscribes (explicit SUBSCRIBE per room it
  has connections in, UNSUBSCRIBE when the last one leaves), so a removal takes effect in about a second instead
  of up to 15 s. If Redis is down or unconfigured the timer alone keeps enforcing access (tested: the service
  logs once per 30 s and carries on).
* **Downgrade without a disconnect.** Hocuspocus drops the updates of a read-only connection, and `Connection.readOnly`
  is mutable (verified in the 4.8.0 source), so a demoted editor's connection is switched to read-only in place;
  the service also sends a stateless message `{"type":"read_only"}` so the browser locks its editor, shows the
  "You can no longer edit this document" notice and offers to reload the document from the server. Edits made in
  the moments before the notice were dropped by the server; the notice says so and offers Copy. There is no
  runtime UPGRADE: a promoted viewer reloads the page.
* **API or database down.** Checks that fail are tolerated for `COLLAB_ACCESS_GRACE_MS` (a blip must not
  disconnect everyone); beyond that every affected connection is closed with **1013** because access can no longer
  be verified. New connections are refused with the temporary denial `unavailable` in the same situation.

### 6.2 Close codes and what the browser does

| Code / denial | Meaning | Browser |
|---|---|---|
| **4401** `session_invalid` | session revoked/expired or user disabled | terminal: "Access lost", **keeps the local text**, no reconnect |
| **4403** `not_a_member` | removed from the room, or the document left it | terminal, same |
| denial `needs_repair` | the document failed validation | terminal; message says it needs repair |
| **1013** | service or its API unavailable | temporary: the provider reconnects with backoff, same `Y.Doc`, local edits are sent afterwards |
| **1001** | server restarting | temporary, same |
| **4408** | timeout | temporary, same |
| **4429** | too many connections for this user | temporary, same |
| **4430** | more than `COLLAB_MESSAGES_PER_SECOND` (100) messages in a second on one connection | temporary, same (the backoff keeps a runaway client from hammering) |
| denial `throttled` | too many connection attempts from one IP | temporary; waits about 30 s |
| denials `unavailable`, `too_many_connections` | | temporary; retried after 3-5 s |

### 6.3 Limits

* **Message rate.** `beforeHandleMessage` (verified to exist in 4.8.0: a throw there makes Hocuspocus drop the
  message) counts messages per connection per second; the 101st closes the socket with 4430 and the others are
  untouched. Normal typing is about 10-20 messages per second (tested at 20/s: no close).
* **Connection throttle.** The official `@hocuspocus/extension-throttle` (MIT) limits connection attempts per IP
  (`COLLAB_CONNECTS_PER_MINUTE`, then a ban for `COLLAB_BAN_MINUTES`). It keys on `X-Real-IP` / `X-Forwarded-For`,
  which any client can send, so unless `COLLAB_TRUST_PROXY_HEADERS=true` (only behind a proxy that sets them)
  the service overwrites them with the TCP peer address before the throttle sees them (tested: rotating the
  headers does not dodge the ban). Its refusal is converted to the temporary denial `throttled` (the extension's
  own refusal would look like a final `forbidden` to the browser).
* Existing limits: message size (1 MiB, 1009), state size (8 MiB, read-only), connections per user (10) and total (500).

### 6.4 Tests (cp 4)

* Service (`collab/test/access.test.ts`, real FastAPI + PostgreSQL + Redis + WebSocket clients, shortened timers):
  removal 4403, revoked session 4401, disabled user 4401, runtime downgrade (socket open, edits dropped, notice,
  nothing sneaky stored, still receives live edits), Redis hint within about a second with the timer idle,
  hint coalescing (40 hints -> at most 3 checks), Redis unreachable (timer still enforces), API failure tolerated
  then 1013, API recovering inside the grace period (nobody disconnected), message-rate 4430 (only the offender),
  ordinary typing, throttle, header spoofing, trusted-proxy mode.
* Backend (`test_collab_internal.py`): the access endpoint (order, every change kind, document of another room,
  statement count, secret and body validation).
* Browser (Playwright): terminal close keeps the text and never reconnects; 1013 reconnects and the text flows
  again; a demoted editor is locked while the page is open and told, the owner keeps editing and the demoted
  user still sees it live.

### 6.5 Known limits (cp 4)

* A check happens within about a second with Redis, up to 15 s without it: a removed member can read for that long.
* The edits of a demoted editor typed between the demotion and the server's notice are dropped (told, not hidden).
* Rate limits are per connection/IP in one process; there is one service instance (the fenced lease), so that is global.

## 7. Migration, assets and cleanup (cp 5)

### 7.1 `migrate_documents_to_collab`

`python -m app.commands.migrate_documents_to_collab [--dry-run] [--document ID] [--room ID] [--limit N]`
seeds `legacy` documents into Yjs in advance (otherwise it happens when someone first opens them). The Python
command does not build Yjs: it asks the RUNNING collaboration service, one document at a time, through
`POST /internal/migrate` (shared secret; the proxy forwards only the WebSocket path). That keeps one seeding
implementation and respects the single-writer lease.

Per document the service answers `migrated`, `would_migrate` (dry run), `already_migrated`,
`skipped_needs_repair`, `invalid` (the seed fails `adapt` or `validateDocument`; the reason is a code and a path,
never content) or `failed`. Rules: it is **idempotent** (migrated documents are no longer legacy; a second run
says "nothing to do" and writes nothing); a failure leaves the document **`legacy`** (an invalid seed is not
marked `needs_repair`, unlike a failed seed on a live open); `documents.content` is never touched; opening a
document while it is being migrated is safe (the seed insert is insert-if-absent). Exit code 0 = nothing failed,
1 = some document was `invalid`/`failed`, 2 = service unreachable or settings missing. Settings: `DATABASE_URL`,
`INTERNAL_API_SECRET`, `COLLAB_SERVICE_URL` (default `http://127.0.0.1:4010`).

Tests (`collab/test/migrate.test.ts`, the real command against the real API, PostgreSQL and service): dry run
changes nothing; real run migrates good documents and leaves the invalid one legacy; idempotent; `--document`
and needs_repair skip; concurrent with a live open; wrong secret and unreachable service; route authentication.
`openDirectConnection` (listed as unverified at checkpoint 1) is now exercised by these tests.

### 7.2 Templates

The five templates are interchange JSON stored as seed content at document creation and go through exactly the
same seeding path (`collab/test/templates.test.ts`: all five reach `ready`, get a seed snapshot, keep real
headings and unique block ids). Documents created before the change are legacy documents like any other.

### 7.3 Asset references and the cleanup guard

References (`document_asset_refs`) are derived **only from validated snapshots** (`services/collab.write_snapshot`
calls `sync_asset_refs` in the snapshot's transaction); a snapshot that mentions a missing, expired or foreign
asset is refused whole. `cleanup_documents` expires unused assets, so it must not delete an image that is in the
live document but not yet in a snapshot. It therefore **skips the assets of a room** that has a `needs_repair`
document, or a document whose Yjs state was stored in the last 10 minutes (the database cannot see sockets; an
open document that is being edited keeps storing, and an open idle one has an accurate snapshot). The command
reports how many rooms it skipped. Tested in `test_assets.py` (needs_repair, recent state, idle, other rooms
unaffected).

### 7.4 Removed code and deprecated objects

Removed: the custom editor and toolbar, the save state machine, `PUT .../content` with `save_content`, its
schemas, the save rate limit and body-limit rule, `document_version_conflict`, the read-only React renderer, the
playground, the TypeScript twin validator `client/src/document-schema.ts` (the Python validator and the golden
fixtures stay), and their tests. Kept and deprecated (no drop, expand-only): `documents.content` (read-only seed),
`content_version`, `content_hash`, `content_updated_at`, the table `document_write_buckets`, the event type
`document_content_updated`. The README lists them.

## 8. AI-ready hooks (cp 6)

No AI is built here: no model, no prompt, no provider. These are the two operations a later AI feature needs from
the collaboration service. They exist and are tested now so the rules are fixed before any caller does.
Both are private HTTP routes of the collaboration service (`POST /internal/ai/read`, `POST /internal/ai/apply`;
shared secret; the reverse proxy forwards only `/collab`). Code: `collab/src/ai-hooks.ts`.

### 8.1 Read

`{document_id, requester_id}` returns the document as it is NOW:

```
{ document_id, yjs_update_counter, content_hash, content (validated Tiptap JSON),
  blocks: [{ id, type, hash, parent_id, text }, ...] }     every node with an id, in document order
```

* pending edits are stored first (`storeNow`), so `yjs_update_counter` is the counter of the stored state the
  answer describes, and it equals `document_yjs_state.update_counter` (tested). A read of an unchanged document does
  not raise the counter: a store of unchanged bytes is skipped (this also made opening/closing a document free).
* `hash` is the block's content hash with every `id` ignored (the editor-schema `blockHash`), `content_hash` is the
  whole document's; a legacy document is seeded first.
* the requester is checked through `POST /internal/collab/can-write` (the shared `decide_write`): viewers,
  strangers, removed members and `needs_repair` documents get 403 with a reason, never content.

### 8.2 Apply a change to one block

`{document_id, requester_id, block_id, base_hash, block}` replaces ONE block, found by its stable id (top level or
nested, e.g. a list item), when and only when:

1. the requester may write to the document **right now** (checked on every call; a role removed a moment ago is refused);
2. the block's current hash equals `base_hash` ("I based this on version H"): otherwise **409 `block_changed`**
   with the current hash and no content - nothing is merged blindly;
3. the whole document with the replacement passes `validateDocument` (else **422 `invalid_change`** with a code
   and a path); the replacement keeps the target's id and missing ids inside it are generated;
4. it fits the limits: body <= 256 KiB, block <= 64 KiB (413), per requester and per document per minute (429;
   every attempt counts, valid or not).

Mechanics: the new block is built by the same y-tiptap code the editor uses, the result is **rehearsed on a copy**
of the Yjs document and must equal what was validated, then applied in ONE synchronous transaction on the live
document. The target is compared once more inside that transaction (no `await` can interleave with a collaborator's
edit there). Only the target block is replaced, so what collaborators type elsewhere is preserved (tested with a
person typing in another block at the same moment).

**Distinct origin.** The transaction runs through a Hocuspocus direct connection whose context is
`{source: "ai-hook", requester_id}`, so its Yjs origin is `{source: "local", context: {source: "ai-hook", ...}}`:
distinguishable from every browser edit (tested by observing `afterTransaction` on the server document).

**Logging.** Ids, sizes, counters and result codes only. A test pushes recognisable strings through read, a
successful apply and a refused apply and asserts none of them (nor the secret) appears in the log.

### 8.3 Tests (cp 6)

* `collab/test/ai-hooks.test.ts` (12): exact counter/hashes, unstored live edits included, legacy seeding, all
  refusals (403/404/409/413/422/429/401), merge with a collaborator, origin, per-call requester check, persistence
  and snapshot, nested block, no content in logs.
* backend: `can-write` endpoint; browser: a server-side change appears live in two open editors, a stale base hash
  is refused while a person is typing in the same block, and typing continues normally afterwards.

### 8.4 What is NOT proven

* Two hook calls racing each other, or a hook call racing a collaborator at the exact same instant, are covered by
  construction (one synchronous transaction with a re-check), not by a stress test.
* Only one service instance exists (the lease), so the per-minute windows are global; they are in memory and reset on restart.

## 9. Final report

See [docs/collaboration-final-report.md](collaboration-final-report.md).
