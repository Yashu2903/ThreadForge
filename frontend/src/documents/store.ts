/**
 * Framework-free document state for one room: the list (metadata only) and the open document.
 *
 * Rules (from the spec, section 7), implemented here so they are testable without React:
 *  - events are coalesced per document by the room client before they reach onSignals();
 *  - a content event for a document that is NOT open only marks the list stale;
 *  - for the open document, a clean local copy is refetched with If-None-Match, at most once per
 *    second per document (a trailing refetch is scheduled if one is asked for sooner);
 *  - an event whose content version is at or below the version we already hold is ignored (our own
 *    acknowledged saves, and repeats).
 * Editing and dirty-copy handling arrive with the saving checkpoint, which extends this class.
 */
import type { DocumentSignals } from "threadforge-client";
import { type Api, ApiError } from "../api";
import type { DocumentFull, DocumentSummary, Page } from "../types";

export const REFETCH_MIN_INTERVAL_MS = 1000;
const PAGE_SIZE = 20;

export interface ListState {
  items: DocumentSummary[];
  nextCursor: string | null;
  loading: boolean;
  stale: boolean;
  error: string | null;
}
export interface OpenState {
  id: string;
  doc: DocumentFull | null;
  etag: string | null;
  loading: boolean;
  error: string | null;
  notFound: boolean;
}
export interface DocumentsState {
  list: ListState;
  open: OpenState | null;
}

export interface StoreDeps {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}
const realDeps: StoreDeps = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export class DocumentStore {
  state: DocumentsState = {
    list: { items: [], nextCursor: null, loading: false, stale: false, error: null },
    open: null,
  };
  private listeners = new Set<() => void>();
  private listSeq = 0;
  private openSeq = 0;
  private lastRefetchAt = -Infinity;
  private trailing: unknown = null;
  private disposed = false;

  constructor(
    private readonly api: Api,
    readonly roomId: string,
    private readonly deps: StoreDeps = realDeps,
  ) {}

  // ---- subscription (React's useSyncExternalStore) ----------------------------------------------------
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): DocumentsState => this.state;
  private set(next: DocumentsState): void {
    this.state = next; // a NEW object each time, so React sees the change
    for (const l of this.listeners) l();
  }
  private patchList(patch: Partial<ListState>): void {
    this.set({ ...this.state, list: { ...this.state.list, ...patch } });
  }
  private patchOpen(patch: Partial<OpenState>): void {
    if (this.state.open) this.set({ ...this.state, open: { ...this.state.open, ...patch } });
  }

  /** Stop all pending work (React StrictMode mounts, unmounts and remounts; activate() undoes this). */
  dispose(): void {
    this.disposed = true;
    if (this.trailing) this.deps.clearTimeout(this.trailing);
    this.trailing = null;
  }
  activate(): void {
    this.disposed = false;
  }

  // ---- the list -------------------------------------------------------------------------------------------
  async loadList(): Promise<void> {
    const seq = ++this.listSeq;
    this.patchList({ loading: true, error: null });
    try {
      const res = await this.api.get<Page<DocumentSummary>>(`/rooms/${this.roomId}/documents?limit=${PAGE_SIZE}`);
      if (seq !== this.listSeq || this.disposed) return;
      this.patchList({ items: res.body.items, nextCursor: res.body.next_cursor, loading: false, stale: false });
    } catch (err) {
      if (seq !== this.listSeq || this.disposed) return;
      this.patchList({ loading: false, error: describe(err) });
    }
  }

  async loadMore(): Promise<void> {
    const cursor = this.state.list.nextCursor;
    if (!cursor || this.state.list.loading) return;
    const seq = ++this.listSeq;
    this.patchList({ loading: true, error: null });
    try {
      const res = await this.api.get<Page<DocumentSummary>>(
        `/rooms/${this.roomId}/documents?limit=${PAGE_SIZE}&cursor=${encodeURIComponent(cursor)}`,
      );
      if (seq !== this.listSeq || this.disposed) return;
      const known = new Set(this.state.list.items.map((d) => d.id));
      this.patchList({
        items: [...this.state.list.items, ...res.body.items.filter((d) => !known.has(d.id))],
        nextCursor: res.body.next_cursor,
        loading: false,
      });
    } catch (err) {
      if (seq !== this.listSeq || this.disposed) return;
      this.patchList({ loading: false, error: describe(err) });
    }
  }

  // ---- the open document ------------------------------------------------------------------------------------
  get openId(): string | null {
    return this.state.open?.id ?? null;
  }

  async open(id: string): Promise<void> {
    if (this.trailing) this.deps.clearTimeout(this.trailing);
    this.trailing = null;
    this.lastRefetchAt = this.deps.now();
    this.set({ ...this.state, open: { id, doc: null, etag: null, loading: true, error: null, notFound: false } });
    await this.fetchOpen(id, false);
  }

  close(): void {
    ++this.openSeq;
    this.set({ ...this.state, open: null });
  }

  private async fetchOpen(id: string, conditional: boolean): Promise<void> {
    const seq = ++this.openSeq;
    const etag = this.state.open?.etag;
    try {
      const res = await this.api.get<DocumentFull>(
        `/rooms/${this.roomId}/documents/${id}`,
        conditional && etag ? { "If-None-Match": etag } : {},
      );
      if (seq !== this.openSeq || this.disposed || this.openId !== id) return;
      if (res.status === 304) return void this.patchOpen({ loading: false });
      this.patchOpen({ doc: res.body, etag: res.headers.get("ETag"), loading: false, error: null, notFound: false });
    } catch (err) {
      if (seq !== this.openSeq || this.disposed || this.openId !== id) return;
      if (err instanceof ApiError && err.code === "document_not_found") {
        return void this.patchOpen({ loading: false, notFound: true, error: null });
      }
      this.patchOpen({ loading: false, error: describe(err) });
    }
  }

  /** Refetch the open document (If-None-Match), at most once per second per document. */
  refreshOpen(): void {
    const id = this.openId;
    if (!id || this.disposed) return;
    const wait = this.lastRefetchAt + REFETCH_MIN_INTERVAL_MS - this.deps.now();
    if (wait > 0) {
      if (!this.trailing) {
        this.trailing = this.deps.setTimeout(() => {
          this.trailing = null;
          this.refreshOpen();
        }, wait);
      }
      return;
    }
    this.lastRefetchAt = this.deps.now();
    void this.fetchOpen(id, true);
  }

  // ---- room events ---------------------------------------------------------------------------------------------
  /** Called by the room client BEFORE its cursor advances; it only starts work, it never throws. */
  onSignals(signals: DocumentSignals): void {
    if (signals.reset) {
      void this.loadList();
      this.refreshOpen();
      return;
    }
    if (signals.listChanged) {
      this.patchList({ stale: true });
      void this.loadList();
    }
    const id = this.openId;
    const change = id ? signals.documents.get(id) : undefined;
    if (id && change) {
      const known = this.state.open?.doc;
      const newerContent = change.contentVersion !== null && (!known || change.contentVersion > known.content_version);
      if (newerContent || change.renamed) this.refreshOpen();
    }
  }
}

function describe(err: unknown): string {
  return err instanceof ApiError ? err.message : "Could not reach the server.";
}
