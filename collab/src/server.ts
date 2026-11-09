/**
 * The collaboration server: Hocuspocus (a Yjs sync server) with ThreadForge's hooks.
 *
 *   browser ──WebSocket /collab──▶ onUpgrade (path, Origin, caps)
 *                                  onAuthenticate (document name, Origin, session cookie → FastAPI,
 *                                                  role → read-only, per-user cap)
 *                                  onLoadDocument (stored Yjs state, or seed it ONCE)
 *                                  … Yjs updates flow between all connections of the document …
 *                                  onStoreDocument (debounced; full Yjs state → FastAPI → PostgreSQL)
 *                                  periodic / final snapshots (validated plain JSON → FastAPI)
 *
 * This process holds NO source of truth: everything durable goes through the internal API under a
 * fenced lease. See docs/collaboration.md for the flows and the failure modes.
 */
import type { IncomingMessage as HttpRequest, ServerResponse } from "node:http";
import type { Duplex } from "node:stream";
import {
  Server,
  type Connection,
  type Document as HocuspocusDocument,
  type Hocuspocus,
  type beforeHandleMessagePayload,
  type onAuthenticatePayload,
  type onConnectPayload,
  type onLoadDocumentPayload,
  type onStoreDocumentPayload,
} from "@hocuspocus/server";
import { Throttle } from "@hocuspocus/extension-throttle";
import * as Y from "yjs";
import { AccessMonitor } from "./access.js";
import { ApiError, InternalApi, type Access } from "./api.js";
import type { CollabConfig } from "./config.js";
import { SeedError, buildSeed, documentIdOf, validatedSnapshot, validatedSnapshotOfState } from "./documents.js";
import { LeaseKeeper } from "./lease.js";
import { createLogger, type Logger } from "./log.js";

/** Close codes used by this service (documented in docs/collaboration.md). */
export const CLOSE = {
  GOING_AWAY: 1001,
  SESSION_INVALID: 4401,
  NOT_A_MEMBER: 4403,
  ACCESS_CHANGED: 4408,
  TOO_MANY_CONNECTIONS: 4429,
  /** more than COLLAB_MESSAGES_PER_SECOND messages in one second on one connection */
  RATE_LIMITED: 4430,
  UNAVAILABLE: 1013,
} as const;

/** Per-connection context. Ids and a role only: never the cookie. */
export interface Ctx {
  userId: string;
  displayName: string;
  sessionId: string;
  role: string;
  canEdit: boolean;
  documentId: string;
  roomId: string;
}

/** Thrown from a hook so Hocuspocus tells the client why (`permission-denied` carries `reason`). */
class Reject extends Error {
  constructor(readonly reason: string) {
    super();
    this.name = "Reject";
  }
}

/** What we remember about a document while it is loaded. */
interface LiveDoc {
  id: string;
  roomId: string;
  /** the counter of the newest state stored (or loaded) */
  counter: number;
  /** encoded state of the newest store that has not been snapshotted yet */
  pendingSnapshot: { bytes: Uint8Array; counter: number } | null;
  lastSnapshotCounter: number;
  snapshotting: boolean;
  snapshotTimer: NodeJS.Timeout | null;
  /** the validated JSON failed the schema: marked needs_repair, raw Yjs still stored */
  invalid: boolean;
  duplicateIdStrikes: number;
  /** state reached the size limit: read-only for everyone */
  frozen: boolean;
  recoveryTimer: NodeJS.Timeout | null;
}

export interface CollabDeps {
  api?: InternalApi;
  logger?: Logger;
  /** Called when the lease is lost; the default exits the process (a stale writer must not linger). */
  onLeaseLost?: () => void;
}

export class CollabServer {
  readonly log: Logger;
  readonly api: InternalApi;
  readonly lease: LeaseKeeper;
  readonly server: Server<Ctx>;
  private readonly live = new Map<string, LiveDoc>();
  readonly access: AccessMonitor;
  private readonly throttle: Throttle;
  private readonly rateWindows = new WeakMap<object, { second: number; count: number }>();
  private openSockets = 0;
  private shuttingDown = false;
  private started = false;

  constructor(
    readonly config: CollabConfig,
    deps: CollabDeps = {},
  ) {
    this.log = deps.logger ?? createLogger(config.logLevel);
    this.api = deps.api ?? new InternalApi(config.apiBaseUrl, config.internalSecret, config.apiTimeoutMs);
    this.lease = new LeaseKeeper(this.api, this.log, {
      renewMs: config.leaseRenewMs,
      graceMs: config.leaseGraceMs,
      onLost: () => (deps.onLeaseLost ?? (() => process.exit(1)))(),
    });
    this.throttle = new Throttle({
      throttle: config.connectsPerMinute,
      consideredSeconds: 60,
      banTime: config.banMinutes,
    });
    this.access = new AccessMonitor(
      this.api,
      {
        connections: () => this.allConnections(),
        close: (connection, code, reason) => this.closeConnection(connection, code, reason),
        downgrade: (connection) => this.downgrade(connection),
      },
      this.log,
      {
        accessCheckMs: config.accessCheckMs,
        accessGraceMs: config.accessGraceMs,
        redisUrl: config.redisUrl,
        redisPrefix: config.redisPrefix,
      },
    );
    this.server = new Server<Ctx>({
      port: config.port,
      address: config.address,
      name: "threadforge-collab",
      quiet: true,
      stopOnSignals: false,
      debounce: config.debounceMs,
      maxDebounce: config.maxDebounceMs,
      // the ws library enforces this BEFORE a message is parsed or authenticated
      websocketOptions: { maxPayload: config.maxMessageBytes },
      onUpgrade: (data) => this.onUpgrade(data.request, data.socket),
      onRequest: (data) => this.onRequest(data.request, data.response),
      onConnect: (data) => this.onConnect(data),
      onAuthenticate: (data) => this.onAuthenticate(data),
      beforeHandleMessage: (data) => this.beforeHandleMessage(data),
      onDisconnect: async () => {
        setTimeout(() => void this.access.syncSubscriptions(), 0);
      },
      onLoadDocument: (data) => this.onLoadDocument(data),
      afterLoadDocument: (data) => this.afterLoadDocument(data),
      connected: (data) => this.connected(data.connection),
      onStoreDocument: (data) => this.onStoreDocument(data),
      beforeUnloadDocument: (data) => this.beforeUnloadDocument(data.documentName, data.document),
      afterUnloadDocument: (data) => this.afterUnloadDocument(data.documentName),
    });
  }

  get hocuspocus(): Hocuspocus<Ctx> {
    return this.server.hocuspocus;
  }
  get port(): number {
    return this.server.address.port;
  }
  /** Loaded documents, for tests and /health. */
  get loadedDocuments(): number {
    return this.live.size;
  }

  async start(): Promise<void> {
    await this.lease.acquire();
    await this.server.listen();
    this.access.start();
    this.started = true;
    this.log.info("collaboration service listening", { port: this.port });
  }

  /**
   * Graceful shutdown: refuse new connections, store everything, close sockets with 1001, wait for the
   * documents to unload (each takes a final snapshot), then release the lease.
   */
  async stop(): Promise<void> {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    this.log.info("shutting down", { documents: this.live.size });
    for (const document of this.hocuspocus.documents.values()) {
      for (const connection of document.getConnections()) {
        try {
          connection.webSocket.close(CLOSE.GOING_AWAY, "service_restarting");
        } catch {
          /* already closing */
        }
      }
    }
    await this.access.stop();
    this.hocuspocus.flushPendingStores();
    await this.server.destroy();
    await this.throttle.onDestroy();
    for (const live of this.live.values()) this.clearTimers(live);
    await this.lease.release();
    this.started = false;
  }

  // ---- HTTP -----------------------------------------------------------------------------------

  private async onRequest(request: HttpRequest, response: ServerResponse): Promise<void> {
    const path = new URL(request.url ?? "/", "http://localhost").pathname;
    if (path === "/health") {
      const healthy = this.started && this.lease.held && !this.shuttingDown;
      response.writeHead(healthy ? 200 : 503, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      response.end(
        JSON.stringify({
          status: healthy ? "ok" : "unavailable",
          lease: this.lease.held,
          documents: this.live.size,
          connections: this.hocuspocus.getConnectionsCount(),
        }),
      );
    } else {
      response.writeHead(404, { "Content-Type": "text/plain" });
      response.end("Not found");
    }
    return Promise.reject(); // handled: stop Hocuspocus' default response
  }

  private refuseUpgrade(socket: Duplex, status: number, text: string): Promise<never> {
    socket.write(`HTTP/1.1 ${status} ${text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
    socket.destroy();
    return Promise.reject(); // empty rejection = "handled", Hocuspocus does not upgrade
  }

  private originAllowed(origin: string | null | undefined): boolean {
    return !!origin && this.config.allowedOrigins.includes(origin);
  }

  private async onUpgrade(request: HttpRequest, socket: Duplex): Promise<void> {
    const path = new URL(request.url ?? "/", "http://localhost").pathname;
    if (path !== this.config.path) return this.refuseUpgrade(socket, 404, "Not Found");
    if (this.shuttingDown) return this.refuseUpgrade(socket, 503, "Service Unavailable");
    if (!this.originAllowed(request.headers.origin)) return this.refuseUpgrade(socket, 403, "Forbidden");
    if (this.openSockets >= this.config.maxConnectionsTotal) return this.refuseUpgrade(socket, 503, "Service Unavailable");
    if (!this.config.trustProxyHeaders) {
      // The throttle keys on X-Real-IP / X-Forwarded-For, which any client can send. Without a trusted
      // proxy in front, replace them with the address of the TCP connection itself.
      request.headers["x-real-ip"] = request.socket.remoteAddress ?? "";
      delete request.headers["x-forwarded-for"];
    }
    this.openSockets += 1;
    socket.once("close", () => {
      this.openSockets -= 1;
    });
  }

  // ---- authentication ---------------------------------------------------------------------------

  private connectionsOfUser(userId: string): number {
    let total = 0;
    for (const document of this.hocuspocus.documents.values()) {
      for (const connection of document.getConnections()) {
        if ((connection.context as Ctx | undefined)?.userId === userId) total += 1;
      }
    }
    return total;
  }

  /** Connection-attempt throttle (per IP). A refusal is a TEMPORARY denial: the client waits and retries. */
  private async onConnect(data: onConnectPayload<Ctx>): Promise<void> {
    try {
      await this.throttle.onConnect(data);
    } catch {
      throw new Reject("throttled");
    }
  }

  /** At most `messagesPerSecond` messages per connection per second; beyond that the socket is closed (4430). */
  private async beforeHandleMessage(data: beforeHandleMessagePayload<Ctx>): Promise<void> {
    const key = data.connection.webSocket as object;
    const second = Math.floor(Date.now() / 1000);
    let window = this.rateWindows.get(key);
    if (!window || window.second !== second) {
      window = { second, count: 0 };
      this.rateWindows.set(key, window);
    }
    window.count += 1;
    if (window.count > this.config.messagesPerSecond) {
      this.log.warn("message rate limit exceeded", { document: data.documentName });
      this.closeConnection(data.connection, CLOSE.RATE_LIMITED, "rate_limited");
      throw new Reject("rate_limited");
    }
  }

  /** Every open document connection (used by the access monitor). */
  private allConnections(): Connection<Ctx>[] {
    const out: Connection<Ctx>[] = [];
    for (const document of this.hocuspocus.documents.values()) out.push(...document.getConnections());
    return out;
  }

  /** The person is still in the room but no longer allowed to write: lock this connection NOW. */
  private downgrade(connection: Connection<Ctx>): void {
    connection.readOnly = true; // Hocuspocus drops a read-only connection's document updates itself
    connection.context.canEdit = false;
    connection.context.role = "viewer";
    try {
      connection.sendStateless(JSON.stringify({ type: "read_only" }));
    } catch {
      /* the socket is closing */
    }
  }

  private async onAuthenticate(data: onAuthenticatePayload<Ctx>): Promise<Ctx> {
    const documentId = documentIdOf(data.documentName);
    if (!documentId) throw new Reject("bad_document");
    // Defence in depth: onUpgrade already checked, but the same socket may be reused by a client.
    if (!this.originAllowed(data.requestHeaders.get("origin"))) throw new Reject("origin");
    let access: Access;
    try {
      access = await this.api.authorize(data.requestHeaders.get("cookie"), documentId);
    } catch (error) {
      throw this.rejectionFor(error);
    }
    if (access.collab_state === "needs_repair") throw new Reject("needs_repair");
    if (this.connectionsOfUser(access.user_id) >= this.config.maxConnectionsPerUser) throw new Reject("too_many_connections");
    // The server, not the client, decides who may write: a read-only connection's updates are
    // ignored by Hocuspocus however the client behaves.
    data.connectionConfig.readOnly = !access.can_edit || (this.live.get(data.documentName)?.frozen ?? false);
    return {
      userId: access.user_id,
      displayName: access.display_name,
      sessionId: access.session_id,
      role: access.role,
      canEdit: access.can_edit,
      documentId: access.document_id,
      roomId: access.room_id,
    };
  }

  private rejectionFor(error: unknown): Reject {
    if (error instanceof ApiError) {
      if (error.status === 401) return new Reject("session_invalid");
      if (error.status === 403) return new Reject("forbidden");
      if (error.status === 404) return new Reject("forbidden");
    }
    this.log.warn("internal api unavailable during authentication", { code: error instanceof ApiError ? error.code : "error" });
    return new Reject("unavailable");
  }

  /** After the connection exists: the strict per-user cap (authentication races can overshoot). */
  private async connected(connection: Connection<Ctx>): Promise<void> {
    void this.access.syncSubscriptions();
    const ctx = connection.context;
    if (this.connectionsOfUser(ctx.userId) > this.config.maxConnectionsPerUser) {
      this.closeConnection(connection, CLOSE.TOO_MANY_CONNECTIONS, "too_many_connections");
    }
  }

  /** Close the connection (and the socket it rides on) with an application close code. */
  closeConnection(connection: Connection, code: number, reason: string): void {
    try {
      connection.close({ code, reason });
      connection.webSocket.close(code, reason);
    } catch {
      /* already closed */
    }
  }

  // ---- load and seed ------------------------------------------------------------------------------

  private async onLoadDocument(data: onLoadDocumentPayload<Ctx>): Promise<void> {
    const id = documentIdOf(data.documentName);
    if (!id) throw new Reject("bad_document");
    try {
      const stored = await this.api.getState(id);
      if (stored.collabState === "needs_repair") throw new Reject("needs_repair");
      let counter: number;
      if (stored.counter >= 0) {
        try {
          Y.applyUpdate(data.document, stored.state);
        } catch {
          await this.markRepair(id, "corrupt_state");
          throw new Reject("needs_repair");
        }
        counter = stored.counter;
      } else {
        counter = await this.seed(id, data.document);
      }
      this.live.set(data.documentName, {
        id,
        roomId: stored.roomId,
        counter,
        pendingSnapshot: null,
        lastSnapshotCounter: -1,
        snapshotting: false,
        snapshotTimer: null,
        invalid: false,
        duplicateIdStrikes: 0,
        frozen: false,
        recoveryTimer: null,
      });
      this.log.info("document loaded", { document: id, counter });
    } catch (error) {
      if (error instanceof Reject) throw error;
      throw this.rejectionFor(error);
    }
  }

  /**
   * Seed a LEGACY document: interchange JSON (or an operator-restored snapshot) to Yjs, stored with
   * insert-if-absent. If another writer got there first (a restart in the middle of a seed) the API
   * answers 409 and we load what is stored instead, so the content is never seeded twice.
   * The seed is applied to the live document only AFTER the database accepted it.
   */
  private async seed(id: string, target: HocuspocusDocument): Promise<number> {
    const seed = await this.api.getSeed(id).catch(async (error: unknown) => {
      if (error instanceof ApiError && error.code === "already_seeded") return null;
      throw error;
    });
    if (seed === null) return this.adoptStored(id, target);
    let built;
    try {
      built = buildSeed(seed.format, seed.content, this.config.limits);
    } catch (error) {
      if (error instanceof SeedError) {
        this.log.error("seed rejected", { document: id, reason: error.reason, detail: error.detail });
        await this.markRepair(id, `seed_${error.reason}`);
        throw new Reject("needs_repair");
      }
      throw error;
    }
    const update = Y.encodeStateAsUpdate(built.ydoc);
    built.ydoc.destroy();
    try {
      await this.api.putState(id, this.lease.token, 0, update, true);
    } catch (error) {
      if (error instanceof ApiError && error.code === "already_seeded") return this.adoptStored(id, target);
      if (error instanceof ApiError && error.code === "stale_lease") this.lease.noteStale();
      throw error;
    }
    Y.applyUpdate(target, update);
    this.log.info("document seeded", { document: id, bytes: update.byteLength, from: seed.format });
    await this.writeSeedSnapshot({ id, counter: 0 }, update);
    return 0;
  }

  private async adoptStored(id: string, target: HocuspocusDocument): Promise<number> {
    const stored = await this.api.getState(id);
    if (stored.counter < 0) throw new ApiError(500, "state_missing_after_seed");
    Y.applyUpdate(target, stored.state);
    return stored.counter;
  }

  private async writeSeedSnapshot(live: Pick<LiveDoc, "id" | "counter">, state: Uint8Array): Promise<void> {
    try {
      const outcome = await validatedSnapshotOfState(state, this.config.limits);
      if (!outcome.ok) return;
      await this.api.snapshot(live.id, this.lease.token, {
        kind: "seed",
        yjs_update_counter: live.counter,
        content: outcome.data.content,
        content_hash: outcome.data.contentHash,
        text_length: outcome.data.textLength,
      });
    } catch (error) {
      // Not fatal: the first periodic snapshot covers it.
      this.log.warn("seed snapshot failed", { document: live.id, code: error instanceof ApiError ? error.code : "error" });
    }
  }

  private async afterLoadDocument(data: onLoadDocumentPayload<Ctx>): Promise<void> {
    const live = this.live.get(data.documentName);
    if (!live) return;
    live.snapshotTimer = setInterval(() => void this.periodicSnapshot(data.documentName), this.config.snapshotIntervalMs);
    live.snapshotTimer.unref?.();
    const size = Y.encodeStateAsUpdate(data.document).byteLength;
    if (size >= this.config.maxStateBytes) this.freeze(data.documentName, data.document);
  }

  // ---- store --------------------------------------------------------------------------------------

  private freeze(documentName: string, document: HocuspocusDocument): void {
    const live = this.live.get(documentName);
    if (!live || live.frozen) return;
    live.frozen = true;
    this.log.error("document reached the maximum state size: read-only for everyone", { document: live.id });
    for (const connection of document.getConnections()) connection.readOnly = true;
  }

  private async onStoreDocument(data: onStoreDocumentPayload<Ctx>): Promise<void> {
    const live = this.live.get(data.documentName);
    if (!live) return;
    const update = Y.encodeStateAsUpdate(data.document);
    if (update.byteLength >= this.config.maxStateBytes) {
      this.freeze(data.documentName, data.document);
      if (update.byteLength > this.config.maxStateBytes) return;
    }
    let lastError: unknown;
    for (let attempt = 0; attempt <= this.config.storeRetries; attempt += 1) {
      const counter = live.counter + 1;
      try {
        await this.api.putState(live.id, this.lease.token, counter, update);
        live.counter = counter;
        live.pendingSnapshot = { bytes: update, counter };
        if (live.recoveryTimer) {
          clearTimeout(live.recoveryTimer);
          live.recoveryTimer = null;
        }
        return;
      } catch (error) {
        lastError = error;
        if (!(error instanceof ApiError)) throw error;
        if (error.code === "stale_lease") {
          this.lease.noteStale();
          throw error;
        }
        if (error.code === "stale_counter") {
          live.counter = Number(error.extra.current_counter ?? live.counter);
          continue;
        }
        if (error.code === "state_too_large" || error.status === 413) {
          this.freeze(data.documentName, data.document);
          return;
        }
        if (!error.transient) throw error;
        this.log.warn("store failed, retrying", { document: live.id, attempt: attempt + 1, code: error.code });
        await new Promise((resolve) => setTimeout(resolve, this.config.storeRetryDelayMs * 2 ** attempt));
      }
    }
    // Out of retries. The document stays in memory (Hocuspocus does not unload it while it is
    // dirty) and we try again later, so a database outage costs time, not data.
    this.log.error("store failed; will try again", { document: live.id });
    if (!live.recoveryTimer && !this.shuttingDown) {
      live.recoveryTimer = setTimeout(() => {
        live.recoveryTimer = null;
        if (this.live.has(data.documentName)) void this.hocuspocus.storeDocumentHooks(data.document, data);
      }, this.config.storeRecoveryDelayMs);
      live.recoveryTimer.unref?.();
    }
    throw lastError;
  }

  // ---- snapshots ------------------------------------------------------------------------------------

  private async periodicSnapshot(documentName: string): Promise<void> {
    const live = this.live.get(documentName);
    if (!live || live.snapshotting || !live.pendingSnapshot || this.shuttingDown) return;
    const { bytes, counter } = live.pendingSnapshot;
    live.snapshotting = true;
    try {
      const outcome = await validatedSnapshotOfState(bytes, this.config.limits);
      await this.finishSnapshot(live, outcome, "periodic", counter);
    } catch (error) {
      this.log.warn("periodic snapshot failed", { document: live.id, code: error instanceof ApiError ? error.code : "error" });
    } finally {
      live.snapshotting = false;
    }
  }

  private async beforeUnloadDocument(documentName: string, document: HocuspocusDocument): Promise<void> {
    const live = this.live.get(documentName);
    if (!live) return;
    this.clearTimers(live);
    if (live.lastSnapshotCounter === live.counter && !live.invalid) return;
    try {
      // nothing is pending (Hocuspocus waits for the store), so the live document IS the stored state
      const outcome = await validatedSnapshot(document, this.config.limits);
      await this.finishSnapshot(live, outcome, "final", live.counter);
    } catch (error) {
      // Never throw from this hook: that would cancel the unload.
      this.log.warn("final snapshot failed", { document: live.id, code: error instanceof ApiError ? error.code : "error" });
    }
  }

  private async afterUnloadDocument(documentName: string): Promise<void> {
    const live = this.live.get(documentName);
    if (live) this.clearTimers(live);
    this.live.delete(documentName);
  }

  private clearTimers(live: LiveDoc): void {
    if (live.snapshotTimer) clearInterval(live.snapshotTimer);
    if (live.recoveryTimer) clearTimeout(live.recoveryTimer);
    live.snapshotTimer = null;
    live.recoveryTimer = null;
  }

  private async finishSnapshot(
    live: LiveDoc,
    outcome: Awaited<ReturnType<typeof validatedSnapshot>>,
    kind: "periodic" | "final",
    counter: number,
  ): Promise<void> {
    if (!outcome.ok) {
      const onlyDuplicates = outcome.errors.every((e) => e.code === "duplicate_id");
      // Two clients can briefly create the same block id; Tiptap's UniqueID repairs it on the next
      // transaction. One failed tick is tolerated for that case only.
      if (onlyDuplicates && live.duplicateIdStrikes < 1) {
        live.duplicateIdStrikes += 1;
        this.log.warn("snapshot has duplicate ids; will re-check", { document: live.id });
        return;
      }
      await this.markInvalid(live, outcome.errors.map((e) => `${e.code}@${e.path}`).slice(0, 5).join(","));
      return;
    }
    live.duplicateIdStrikes = 0;
    try {
      await this.api.snapshot(live.id, this.lease.token, {
        kind,
        yjs_update_counter: counter,
        content: outcome.data.content,
        content_hash: outcome.data.contentHash,
        text_length: outcome.data.textLength,
      });
    } catch (error) {
      if (error instanceof ApiError && error.code === "asset_not_found") {
        await this.markInvalid(live, "assets_missing");
        return;
      }
      if (error instanceof ApiError && error.code === "stale_lease") this.lease.noteStale();
      throw error;
    }
    live.lastSnapshotCounter = counter;
    if (live.pendingSnapshot?.counter === counter) live.pendingSnapshot = null;
    if (live.invalid) {
      // a live document that is valid again leaves needs_repair on its own
      await this.api.mark(live.id, this.lease.token, "ready", "valid_again");
      live.invalid = false;
      this.log.info("document valid again", { document: live.id });
    }
  }

  private async markInvalid(live: LiveDoc, why: string): Promise<void> {
    if (live.invalid) return;
    live.invalid = true;
    this.log.error("snapshot invalid: document needs repair", { document: live.id, why });
    await this.markRepair(live.id, "snapshot_invalid");
  }

  private async markRepair(id: string, reason: string): Promise<void> {
    try {
      await this.api.mark(id, this.lease.token, "needs_repair", reason);
    } catch (error) {
      this.log.error("could not mark needs_repair", { document: id, code: error instanceof ApiError ? error.code : "error" });
    }
  }
}
