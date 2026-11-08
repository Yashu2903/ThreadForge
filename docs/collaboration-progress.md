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

**Not yet verified (checked when used):** Hocuspocus hook semantics (`onAuthenticate`, read-only connections,
debounce options, `beforeHandleMessage`, `openDirectConnection`, HTTP `/health` beside WebSocket), y-tiptap JSON helpers,
CollaborationCaret API, `HocuspocusProvider` unsynced-changes signal.
