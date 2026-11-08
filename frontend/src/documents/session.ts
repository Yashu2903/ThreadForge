/**
 * Saving one open document: the state machine behind "Saved / Unsaved changes / Saving / Save
 * failed / Conflict / Read-only". Framework-free; React only displays its state.
 *
 * RULES (from the spec, section 10):
 *  - "Saved" means the backend committed (HTTP 200 with a content_version), never merely "sent".
 *  - Explicit save (button, Ctrl/Cmd+S) plus autosave ~1 s after edits settle; never during IME
 *    composition (the editor makes no commits then, and a timer that fires mid-composition waits).
 *  - ONE save in flight. A response acknowledges only the SNAPSHOT that was submitted: typing during
 *    a save keeps the newer local edits, and the next save uses the version the response returned.
 *  - 429: autosave pauses for Retry-After. Network failure or 5xx: local state is kept, retries back
 *    off (2 s doubling to 30 s) and there is a manual Retry.
 *  - 409 (or a document_content_updated event for a version we do not have while we hold unsaved
 *    work): STOP saving, keep the local text, say another session saved a newer version. Nothing is
 *    merged and nothing is forced: "Load latest" discards local edits only after the user confirms,
 *    and "Copy my text" is the recovery aid.
 *  - A clean copy follows newer server versions automatically. Adopting one resets undo history,
 *    because the old snapshots describe a document that no longer exists.
 *  - Losing edit rights (role downgrade, removal) stops saving, makes the editor read-only and KEEPS
 *    the unsaved text in memory for copy-out.
 * There is no offline persistence and no crash recovery: unsaved text lives in this tab only, and the
 * page warns before it is closed or reloaded (beforeunload) while anything is unsaved.
 */
import { validateDocument, InvalidDocument, type Limits } from "threadforge-client/document-schema";
import { type Api, ApiError } from "../api";
import { type Doc, indexOf, blockLength, textBlock } from "../editor/doc";
import { selectionText } from "../editor/commands";
import type { DocumentFull } from "../types";

export type SaveStatus = "saved" | "unsaved" | "saving" | "failed" | "conflict" | "read-only";

export interface SessionState {
  status: SaveStatus;
  message: string | null;
  /** the server version our last acknowledged save (or load) corresponds to */
  contentVersion: number;
  /** the newer server version we know exists while in conflict */
  serverVersion: number | null;
  dirty: boolean;
  readOnlyReason: ReadOnlyReason | null;
}
export type ReadOnlyReason = "viewer" | "role_changed" | "access_lost" | "mobile";

export interface EditorLike {
  getDocument(): Doc;
  setDocument(doc: Doc): void;
  setReadOnly(value: boolean): void;
  readonly isComposing: boolean;
}

export interface SessionDeps {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}
const realDeps: SessionDeps = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export const AUTOSAVE_MS = 1000;
const COMPOSITION_RECHECK_MS = 250;
const BACKOFF_START_MS = 2000;
const BACKOFF_MAX_MS = 30_000;

export interface SessionOptions {
  api: Api;
  roomId: string;
  initial: DocumentFull;
  limits?: Limits;
  deps?: SessionDeps;
  autosaveMs?: number;
}

export class DocumentSession {
  readonly docId: string;
  private readonly api: Api;
  private readonly roomId: string;
  private readonly deps: SessionDeps;
  private readonly limits?: Limits;
  private readonly autosaveMs: number;
  private editor: EditorLike | null = null;
  private savedDoc: Doc;
  private contentVersion: number;
  private status: SaveStatus = "saved";
  private message: string | null = null;
  private serverVersion: number | null = null;
  private readOnlyReason: ReadOnlyReason | null = null;
  private inFlight: Promise<void> | null = null;
  private saveAgain = false;
  private pendingEvents: number[] = [];
  private timer: unknown = null;
  private retryTimer: unknown = null;
  private backoff = BACKOFF_START_MS;
  private disposed = false;
  private listeners = new Set<() => void>();
  private snapshot: SessionState;

  constructor(options: SessionOptions) {
    this.api = options.api;
    this.roomId = options.roomId;
    this.docId = options.initial.id;
    this.deps = options.deps ?? realDeps;
    this.limits = options.limits;
    this.autosaveMs = options.autosaveMs ?? AUTOSAVE_MS;
    this.savedDoc = options.initial.content as Doc;
    this.contentVersion = options.initial.content_version;
    this.snapshot = this.compute();
  }

  // ---- subscription -----------------------------------------------------------------------------------------
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): SessionState => this.snapshot;
  private compute(): SessionState {
    return {
      status: this.status,
      message: this.message,
      contentVersion: this.contentVersion,
      serverVersion: this.serverVersion,
      dirty: this.isDirty(),
      readOnlyReason: this.readOnlyReason,
    };
  }
  private publish(): void {
    this.snapshot = this.compute();
    for (const l of this.listeners) l();
  }
  private statusIs(status: SaveStatus): boolean {
    return this.status === status; // a method, so TypeScript does not narrow across awaits
  }
  private set(status: SaveStatus, message: string | null = null): void {
    this.status = status;
    this.message = message;
    this.publish();
  }

  // ---- wiring -----------------------------------------------------------------------------------------------
  attach(editor: EditorLike): void {
    this.editor = editor;
    if (this.readOnlyReason) editor.setReadOnly(true);
  }

  isDirty(): boolean {
    return !!this.editor && this.editor.getDocument() !== this.savedDoc;
  }
  /** Anything the user could lose by navigating away or replacing the document. */
  get hasUnsavedWork(): boolean {
    return this.isDirty() || this.status === "saving";
  }

  /** Stop timers and ignore late answers. activate() undoes it (React StrictMode mounts, unmounts, mounts). */
  dispose(): void {
    this.disposed = true;
    this.clearTimers();
  }
  activate(): void {
    this.disposed = false;
  }
  private clearTimers(): void {
    if (this.timer) this.deps.clearTimeout(this.timer);
    if (this.retryTimer) this.deps.clearTimeout(this.retryTimer);
    this.timer = this.retryTimer = null;
  }

  // ---- edits and autosave ----------------------------------------------------------------------------------------
  /** Called by the editor for every committed change (never during composition). */
  onEditorChange(): void {
    if (this.disposed || this.readOnlyReason) return;
    if (this.status === "conflict") return void this.publish(); // local edits continue; nothing is saved
    if (this.status === "failed" && this.retryTimer) return void this.publish(); // the retry timer decides
    if (this.status !== "saving") this.set("unsaved");
    else this.publish();
    this.scheduleAutosave();
  }

  private scheduleAutosave(): void {
    if (this.timer) this.deps.clearTimeout(this.timer);
    this.timer = this.deps.setTimeout(() => {
      this.timer = null;
      if (this.editor?.isComposing) return void this.recheckLater();
      void this.saveNow();
    }, this.autosaveMs);
  }
  private recheckLater(): void {
    this.timer = this.deps.setTimeout(() => {
      this.timer = null;
      if (this.editor?.isComposing) return this.recheckLater();
      void this.saveNow();
    }, COMPOSITION_RECHECK_MS);
  }

  /** Explicit save (button, Ctrl/Cmd+S) and the autosave target. */
  async saveNow(): Promise<void> {
    if (this.disposed || !this.editor || this.readOnlyReason || this.status === "conflict") return;
    if (this.timer) {
      this.deps.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.inFlight) {
      this.saveAgain = true;
      return this.inFlight;
    }
    if (this.editor.isComposing) return void this.recheckLater();
    if (!this.isDirty()) {
      if (this.status !== "saved") this.set("saved");
      return;
    }
    this.inFlight = this.performSave().finally(() => {
      this.inFlight = null;
    });
    await this.inFlight;
    if (this.saveAgain && !this.disposed) {
      this.saveAgain = false;
      if (this.isDirty() && !this.statusIs("conflict") && !this.statusIs("failed")) await this.saveNow();
    }
  }

  private async performSave(): Promise<void> {
    const editor = this.editor!;
    const snapshot = editor.getDocument();
    let content: Doc;
    try {
      content = validateDocument(snapshot, this.limits).content as Doc; // the client canonicalizes first
    } catch (err) {
      const where = err instanceof InvalidDocument ? ` (${err.path || "document"}: ${err.detail})` : "";
      return this.set("failed", `This document cannot be saved as it is${where}. Your text is still here.`);
    }
    this.set("saving");
    try {
      const res = await this.api.send<{
        content_version: number;
        unchanged: boolean;
        canonical_changed: boolean;
        content: Doc | null;
      }>("PUT", `/rooms/${this.roomId}/documents/${this.docId}/content`, {
        schema_version: 1,
        expected_content_version: this.contentVersion,
        content,
      });
      if (this.disposed) return;
      this.contentVersion = res.body.content_version;
      this.savedDoc = snapshot; // the response acknowledges THIS snapshot, not whatever is in the editor now
      if (res.body.canonical_changed && res.body.content && editor.getDocument() === snapshot) {
        editor.setDocument(res.body.content); // only when there are no newer local edits
        this.savedDoc = res.body.content;
      }
      this.backoff = BACKOFF_START_MS;
      this.afterSave();
    } catch (err) {
      if (this.disposed) return;
      this.afterFailure(err);
    }
  }

  private afterSave(): void {
    // events that arrived while the save was in flight: versions up to ours are our own save
    const foreign = this.pendingEvents.filter((v) => v > this.contentVersion);
    this.pendingEvents = [];
    if (foreign.length > 0) {
      if (this.isDirty()) return this.enterConflict(Math.max(...foreign));
      this.serverVersion = Math.max(...foreign); // clean: the store refetches and onServerDocument adopts it
    }
    if (this.isDirty()) {
      this.set("unsaved");
      this.scheduleAutosave(); // typing happened during the save: the next save uses the new version
    } else this.set("saved");
  }

  private afterFailure(err: unknown): void {
    if (err instanceof ApiError) {
      if (err.code === "document_version_conflict") {
        const current = Number(err.extra.current_content_version);
        return this.enterConflict(Number.isFinite(current) ? current : null);
      }
      if (err.status === 403 || err.status === 404) {
        return this.setReadOnly(err.status === 404 ? "access_lost" : "role_changed");
      }
      if (err.status === 429) {
        const seconds = err.retryAfter ?? 30;
        this.set("failed", `Saving is paused for ${seconds} seconds because you saved too often. Your text is safe here.`);
        return this.scheduleRetry(seconds * 1000);
      }
      if (err.status === 422 || err.status === 413) {
        return this.set("failed", `The server did not accept this document: ${err.message} Your text is still here.`);
      }
    }
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, BACKOFF_MAX_MS);
    this.set("failed", `Could not save. Your text is safe here; trying again in ${Math.round(delay / 1000)} seconds.`);
    this.scheduleRetry(delay);
  }

  private scheduleRetry(ms: number): void {
    if (this.retryTimer) this.deps.clearTimeout(this.retryTimer);
    this.retryTimer = this.deps.setTimeout(() => {
      this.retryTimer = null;
      if (this.status === "failed") {
        this.status = "unsaved";
        void this.saveNow();
      }
    }, ms);
  }

  /** Manual Retry after a failure. */
  retryNow(): void {
    if (this.status !== "failed") return;
    if (this.retryTimer) this.deps.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.status = "unsaved";
    void this.saveNow();
  }

  // ---- other sessions ------------------------------------------------------------------------------------------------
  private enterConflict(serverVersion: number | null): void {
    this.clearTimers();
    this.serverVersion = serverVersion;
    this.set("conflict", "Another session saved a newer version of this document. Your changes have not been saved.");
  }

  /** A document_content_updated event for this document, with the content version it announces. */
  onServerEvent(version: number): void {
    if (this.disposed || this.readOnlyReason || version <= this.contentVersion) return; // ours, old, or we cannot edit anyway
    if (this.inFlight) return void this.pendingEvents.push(version); // judged when the save answers
    if (this.status === "conflict") {
      this.serverVersion = Math.max(this.serverVersion ?? 0, version);
      return this.publish();
    }
    if (this.isDirty()) return this.enterConflict(version); // proactive: do not wait for the 409
    this.serverVersion = version; // clean: the document store refetches, onServerDocument adopts
    this.publish();
  }

  /** The store fetched this document from the server. A clean copy adopts a newer one; a dirty one never does. */
  onServerDocument(doc: DocumentFull): void {
    if (this.disposed || doc.id !== this.docId || doc.content_version <= this.contentVersion) return;
    if (this.readOnlyReason && this.isDirty()) return; // keep the unsaved text for copy-out
    if (this.isDirty() || this.inFlight) {
      if (this.status !== "conflict") this.enterConflict(doc.content_version);
      else {
        this.serverVersion = doc.content_version;
        this.publish();
      }
      return;
    }
    this.adopt(doc, "Updated to the latest version saved by someone else.");
  }

  /** "Load latest", after the user confirmed discarding their local changes. */
  loadLatest(doc: DocumentFull): void {
    this.adopt(doc, null);
  }

  private adopt(doc: DocumentFull, message: string | null): void {
    this.clearTimers();
    this.pendingEvents = [];
    this.serverVersion = null;
    this.contentVersion = doc.content_version;
    this.savedDoc = doc.content as Doc;
    this.editor?.setDocument(doc.content as Doc); // resets undo history: the old snapshots describe a document that no longer exists
    this.set(this.readOnlyReason ? "read-only" : "saved", message);
  }

  // ---- access ---------------------------------------------------------------------------------------------------------
  setReadOnly(reason: ReadOnlyReason | null): void {
    if (this.disposed) return;
    this.readOnlyReason = reason;
    this.editor?.setReadOnly(reason !== null);
    if (reason) {
      this.clearTimers();
      this.pendingEvents = [];
      this.set(
        "read-only",
        {
          viewer: "You can read this document but not edit it.",
          role_changed: "You can no longer edit this document. Anything you had not saved is still on this page: copy it out.",
          access_lost: "You no longer have access to this room. Anything you had not saved is still on this page: copy it out.",
          mobile: "Editing is not supported in mobile browsers; you can view this document.",
        }[reason],
      );
    } else this.set(this.isDirty() ? "unsaved" : "saved");
    if (!reason && this.isDirty()) this.scheduleAutosave();
  }

  // ---- recovery ---------------------------------------------------------------------------------------------------------
  /** The local document as plain text, for "Copy my text". */
  localText(): string {
    const doc = this.editor?.getDocument() ?? this.savedDoc;
    const order = indexOf(doc).order;
    const last = order[order.length - 1]!;
    return selectionText(doc, {
      anchor: { id: order[0]!, offset: 0 },
      focus: { id: last, offset: blockLength(textBlock(doc, last)) },
    });
  }
}
