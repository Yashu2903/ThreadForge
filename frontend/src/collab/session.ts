/**
 * One live connection to one document: a Yjs document plus a Hocuspocus provider.
 *
 * LIFECYCLE (the rules the spec asks for):
 *   * one CollabSession per OPEN document; created when the document is opened;
 *   * destroyed when the document changes, the room changes, or the component unmounts;
 *   * destroying disconnects the socket and frees the Y.Doc (no leaked sockets or listeners);
 *   * the connection is same-origin (`/collab`), authenticated by the session COOKIE the browser
 *     already sends, so no token is handled here (the "token" below is a fixed non-secret marker
 *     because the protocol only authenticates when a token is present).
 *
 * There is no offline persistence (no y-indexeddb): unsynced changes live only in this tab.
 * The class is framework free; React reads it through subscribe/getSnapshot.
 */
import { HocuspocusProvider } from "@hocuspocus/provider";
import * as Y from "yjs";
import { RETRY_DELAY_MS, type RawState, type SyncStatus, TERMINAL_CODES, TERMINAL_DENIALS, deriveStatus } from "./status";

export interface CollabOptions {
  /** ws:// or wss:// URL of the collaboration endpoint, e.g. wss://app.example.com/collab */
  url: string;
  /** the Hocuspocus document name, "doc:<id>" */
  documentName: string;
  canEdit: boolean;
  /** tests inject a WebSocket implementation that sends cookies/Origin; browsers use their own */
  WebSocketPolyfill?: unknown;
}

export interface CollabSnapshot extends SyncStatus {
  /** the last close code, for diagnostics and tests */
  lastCloseCode: number | null;
  /** why the server ended our access (session_invalid, forbidden, needs_repair, ...), if it did */
  denial: string | null;
  /** the server turned this connection read-only while it was open (our role was lowered) */
  demoted: boolean;
  /** a stable value that changes whenever the snapshot does (React compares it) */
  version: number;
}

export function collabUrl(path: string, location: Pick<Location, "protocol" | "host"> = window.location): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`;
}

export class CollabSession {
  readonly ydoc = new Y.Doc();
  readonly provider: HocuspocusProvider;
  private raw: RawState;
  private snapshot: CollabSnapshot;
  private listeners = new Set<() => void>();
  private lastCloseCode: number | null = null;
  private denial: string | null = null;
  private demoted = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;
  private version = 0;
  private lastSynced = false;

  constructor(readonly options: CollabOptions) {
    this.raw = {
      connection: "connecting",
      synced: false,
      unsynced: 0,
      scope: undefined,
      canEdit: options.canEdit,
      accessLost: false,
    };
    this.snapshot = this.compute();
    // The websocket options (WebSocketPolyfill, maxAttempts) are accepted at runtime but are not
    // all in the TypeScript type, so the configuration object is built loosely.
    const config: Record<string, unknown> = {
      url: options.url,
      name: options.documentName,
      document: this.ydoc,
      token: "cookie",
      maxAttempts: 0, // unlimited reconnect attempts (with the library's backoff)
      onStatus: ({ status }: { status: string }) => this.patch({ connection: status as RawState["connection"] }),
      onAuthenticated: ({ scope }: { scope: "read-write" | "readonly" }) => this.patch({ scope }),
      onAuthenticationFailed: ({ reason }: { reason: string }) => this.denied(reason),
      onSynced: ({ state }: { state: boolean }) => {
        this.lastSynced = state;
        this.patch({ synced: state });
      },
      // The server tells us when it makes THIS connection read-only at runtime (our role was lowered)
      onStateless: ({ payload }: { payload: string }) => {
        try {
          if ((JSON.parse(payload) as { type?: unknown }).type === "read_only") {
            this.demoted = true;
            this.patch({ scope: "readonly" });
          }
        } catch {
          /* not for us */
        }
      },
      onUnsyncedChanges: ({ number }: { number: number }) => this.patch({ unsynced: number }),
      onClose: ({ event }: { event: { code: number } }) => {
        this.lastCloseCode = event.code;
        if (TERMINAL_CODES.includes(event.code)) this.terminal();
        else this.patch({});
      },
    };
    if (options.WebSocketPolyfill) config.WebSocketPolyfill = options.WebSocketPolyfill;
    this.provider = new HocuspocusProvider(config as never);
  }

  // ---- React glue ----------------------------------------------------------------------------
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): CollabSnapshot => this.snapshot;

  /** Local edits the server has not acknowledged (for the leave-the-page warning and switching). */
  get unsyncedChanges(): number {
    return this.provider.unsyncedChanges;
  }

  /** Text must stay available for copying after access is lost: the Y.Doc is NOT cleared. */
  get hasLocalChanges(): boolean {
    return this.snapshot.atRisk > 0;
  }

  setCanEdit(canEdit: boolean): void {
    if (this.raw.canEdit !== canEdit) this.patch({ canEdit });
  }

  /**
   * The server refused (or ended) our access. Some reasons are final (the session ended, we are not
   * a member, the document needs repair); the rest (the server cannot reach its API right now, or
   * too many tabs) are retried after a pause. The retry reconnects the SAME Y.Doc, so local edits
   * are kept and sent once access is granted again.
   */
  private denied(reason: string): void {
    if (this.destroyed) return;
    if (TERMINAL_DENIALS.includes(reason)) {
      this.denial = reason;
      return this.terminal();
    }
    this.raw = { ...this.raw, connection: "disconnected", synced: false };
    this.publish();
    this.retryTimer ??= setTimeout(() => {
      this.retryTimer = null;
      if (this.destroyed || this.raw.accessLost) return;
      this.provider.disconnect();
      void this.provider.connect();
    }, (RETRY_DELAY_MS[reason] ?? 3000) + Math.random() * 2000);
  }

  /** Stop reconnecting after the server ended our access. Local text is kept. */
  private terminal(): void {
    if (this.destroyed) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.raw = { ...this.raw, accessLost: true };
    try {
      this.provider.disconnect();
    } catch {
      /* already closed */
    }
    this.publish();
  }

  private patch(change: Partial<RawState>): void {
    if (this.destroyed) return;
    this.raw = { ...this.raw, ...change };
    this.publish();
  }

  private compute(): CollabSnapshot {
    return { ...deriveStatus(this.raw), lastCloseCode: this.lastCloseCode, denial: this.denial, demoted: this.demoted, version: this.version };
  }

  private publish(): void {
    const next = this.compute();
    const prev = this.snapshot;
    if (prev.kind === next.kind && prev.label === next.label && prev.atRisk === next.atRisk && prev.editable === next.editable && prev.lastCloseCode === next.lastCloseCode && prev.denial === next.denial && prev.demoted === next.demoted) {
      return;
    }
    this.version += 1;
    this.snapshot = { ...next, version: this.version };
    for (const l of this.listeners) l();
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.listeners.clear();
    try {
      this.provider.destroy();
    } finally {
      this.ydoc.destroy();
    }
  }

  get isDestroyed(): boolean {
    return this.destroyed;
  }

  /** for tests */
  get everSynced(): boolean {
    return this.lastSynced;
  }
}
