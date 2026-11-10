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
import { createHash, timingSafeEqual } from "node:crypto";
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
import { AiHooks } from "./ai-hooks.js";
import { ApiError, InternalApi, type Access, type ApplyReceipt } from "./api.js";
import { ProposalApplier } from "./apply.js";
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
  /** hash of the newest stored state: an unchanged document is not stored again */
  lastStoredHash: string | null;
  /** the validated JSON failed the schema: marked needs_repair, raw Yjs still stored */
  invalid: boolean;
  duplicateIdStrikes: number;
  /** state reached the size limit: read-only for everyone */
  frozen: boolean;
  recoveryTimer: NodeJS.Timeout | null;
  /** when a PERSON's edit last reached this document (AI applies and server-side changes do not count) */
  lastHumanUpdateAt: number;
  /** an AI apply's receipt, carried by the next store so the database records it in the same transaction */
  receipt: ApplyReceipt | null;
}

/** What `POST /internal/migrate` answers for one document. `detail` is a code or a path, never content. */
export type DirectHandle = Awaited<ReturnType<Hocuspocus<Ctx>["openDirectConnection"]>>;

export interface MigrateResult {
  result: "migrated" | "would_migrate" | "already_migrated" | "skipped_needs_repair" | "invalid" | "failed";
  detail?: string;
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
  readonly ai: AiHooks;
  readonly applier: ProposalApplier;
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
    this.ai = new AiHooks(this, this.api, config, this.log);
    this.applier = new ProposalApplier(this, this.api, config, this.log);
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
    this.applier.startSweeper();
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
    this.applier.stopSweeper();
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
    } else if (
      request.method === "POST" &&
      ["/internal/migrate", "/internal/ai/read", "/internal/ai/apply", "/internal/ai/apply-proposal"].includes(path)
    ) {
      await this.onInternalRequest(path, request, response);
    } else {
      response.writeHead(404, { "Content-Type": "text/plain" });
      response.end("Not found");
    }
    return Promise.reject(); // handled: stop Hocuspocus' default response
  }

  /**
   * The private HTTP routes of the service (secret-protected; the reverse proxy forwards only the
   * WebSocket path, so they are reachable from the private network only):
   *   POST /internal/migrate     {document_id, dry_run}                       seed one legacy document
   *   POST /internal/ai/read     {document_id, requester_id}                  AI hook: read
   *   POST /internal/ai/apply    {document_id, requester_id, block_id, base_hash, block}   AI hook: change
   *   POST /internal/ai/apply-proposal {proposal_id, document_id, user_id}      apply a reviewed proposal
   */
  private async onInternalRequest(path: string, request: HttpRequest, response: ServerResponse): Promise<void> {
    const send = (status: number, body: unknown) => {
      response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      response.end(JSON.stringify(body));
    };
    const given = Buffer.from(String(request.headers["x-internal-secret"] ?? ""));
    const expected = Buffer.from(this.config.internalSecret);
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) return send(401, { error: "unauthenticated" });
    if (this.shuttingDown) return send(503, { error: "shutting_down" });
    const limit = path === "/internal/migrate" ? 4096 : this.config.aiMaxBodyBytes;
    let raw = "";
    let size = 0;
    for await (const chunk of request) {
      size += (chunk as Buffer).length;
      if (size > limit) return send(413, { error: "too_large" });
      raw += chunk;
    }
    let body: Record<string, unknown>;
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
      body = parsed as Record<string, unknown>;
    } catch {
      return send(422, { error: "invalid_json" });
    }
    if (path === "/internal/ai/read") {
      const reply = await this.ai.read(body);
      return send(reply.status, reply.body);
    }
    if (path === "/internal/ai/apply") {
      const reply = await this.ai.apply(body);
      return send(reply.status, reply.body);
    }
    if (path === "/internal/ai/apply-proposal") {
      const reply = await this.applier.apply(body);
      return send(reply.status, reply.body);
    }
    if (typeof body.document_id !== "string" || !/^[0-9a-f-]{36}$/.test(body.document_id)) return send(422, { error: "invalid_document_id" });
    send(200, await this.migrate(body.document_id, body.dry_run === true));
  }

  /**
   * Run `fn` against the LIVE document (loading, and if necessary seeding, it first) through an internal
   * direct connection, so server-side callers see and change exactly what people in browsers see.
   * `context` becomes the transaction origin of anything `direct.transact` does.
   */
  async withDocument<T>(
    documentId: string,
    context: Record<string, unknown>,
    fn: (handle: { document: HocuspocusDocument; name: string; direct: DirectHandle }) => Promise<T>,
  ): Promise<T> {
    const name = `doc:${documentId}`;
    const direct = await this.hocuspocus.openDirectConnection(name, context as never);
    try {
      if (!direct.document || !this.live.has(name)) throw new Error("document not loaded");
      return await fn({ document: direct.document, name, direct });
    } finally {
      await direct.disconnect();
    }
  }

  /**
   * Store the live document NOW (unless nothing changed since the last store) and say exactly which
   * bytes were stored under which counter, so a caller can describe that state precisely.
   */
  async storeNow(
    name: string,
    document: HocuspocusDocument,
    receipt?: ApplyReceipt,
  ): Promise<{ counter: number; bytes: Uint8Array; durable: boolean }> {
    const live = this.live.get(name);
    if (!live) throw new Error("document not loaded");
    if (receipt) live.receipt = receipt;
    try {
      await this.onStoreDocument({ documentName: name, document } as never); // a no-op if nothing changed
    } finally {
      live.receipt = null;
    }
    const bytes = Y.encodeStateAsUpdate(document);
    // onStoreDocument gives up quietly after its retries (the document stays in memory and is retried later):
    // "durable" is only true when the newest stored bytes ARE these bytes.
    const durable = live.lastStoredHash === createHash("sha256").update(bytes).digest("hex");
    return { counter: live.counter, bytes, durable };
  }

  /** Milliseconds since a person's edit last reached this document (Infinity if none since it loaded). */
  msSinceHumanUpdate(documentName: string): number {
    const live = this.live.get(documentName);
    return live ? Date.now() - live.lastHumanUpdateAt : Number.POSITIVE_INFINITY;
  }

  /** Distinct users with an open connection to this document, with their roles. */
  connectedUsers(documentName: string): { userId: string; displayName: string; canEdit: boolean }[] {
    const document = this.hocuspocus.documents.get(documentName);
    if (!document) return [];
    const seen = new Map<string, { userId: string; displayName: string; canEdit: boolean }>();
    for (const connection of document.getConnections()) {
      const ctx = connection.context as Ctx | undefined;
      if (ctx?.userId && !seen.has(ctx.userId)) seen.set(ctx.userId, { userId: ctx.userId, displayName: ctx.displayName, canEdit: ctx.canEdit });
    }
    return [...seen.values()];
  }

  /**
   * Seed one legacy document NOW through the same path a first visitor would trigger (onLoadDocument),
   * so there is exactly one seeding implementation. The seed is validated FIRST, outside that path: an
   * invalid one is reported and the document stays `legacy` (it is not marked needs_repair, which is what
   * a failed seed on a live open would do).
   */
  async migrate(documentId: string, dryRun: boolean): Promise<MigrateResult> {
    try {
      const stored = await this.api.getState(documentId);
      if (stored.collabState === "needs_repair") return { result: "skipped_needs_repair" };
      if (stored.counter >= 0 || stored.collabState === "ready") return { result: "already_migrated" };
      const seed = await this.api.getSeed(documentId);
      try {
        buildSeed(seed.format, seed.content, this.config.limits);
      } catch (error) {
        if (error instanceof SeedError) return { result: "invalid", detail: `${error.reason}: ${error.detail}` };
        throw error;
      }
      if (dryRun) return { result: "would_migrate" };
      const ctx = { userId: "migration", displayName: "migration", sessionId: "", role: "owner", canEdit: false, documentId, roomId: stored.roomId };
      const direct = await this.hocuspocus.openDirectConnection(`doc:${documentId}`, ctx);
      await direct.disconnect();
      const after = await this.api.getState(documentId);
      return after.counter >= 0 && after.collabState === "ready"
        ? { result: "migrated" }
        : { result: "failed", detail: "seed_not_stored" };
    } catch (error) {
      if (error instanceof ApiError) return { result: "failed", detail: error.status === 404 ? "document_not_found" : error.code };
      this.log.error("migrate failed", { error: error instanceof Error ? error.name : "error" });
      return { result: "failed", detail: "unexpected_error" };
    }
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
        // what was just loaded IS the stored state: opening a document must not make it look changed
        lastStoredHash: createHash("sha256").update(Y.encodeStateAsUpdate(data.document)).digest("hex"),
        lastSnapshotCounter: -1,
        snapshotting: false,
        snapshotTimer: null,
        invalid: false,
        duplicateIdStrikes: 0,
        frozen: false,
        recoveryTimer: null,
        lastHumanUpdateAt: 0,
        receipt: null,
      });
      // a PERSON's edit arrives with the connection as the transaction origin; a direct (server-side) change,
      // like an AI apply, has `{source: "local", ...}`. Only people reset the "quiet" clock.
      data.document.on("update", (_update: Uint8Array, origin: unknown) => {
        const isServerSide = typeof origin === "object" && origin !== null && (origin as { source?: string }).source === "local";
        const live = this.live.get(data.documentName);
        if (live && !isServerSide) live.lastHumanUpdateAt = Date.now();
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
    // Nothing changed since the last store (an awareness-only tick, a read, a disconnect): storing again
    // would only raise the counter, and a reader could no longer say "this counter IS this state".
    if (live.lastStoredHash === createHash("sha256").update(update).digest("hex")) return;
    if (update.byteLength >= this.config.maxStateBytes) {
      this.freeze(data.documentName, data.document);
      if (update.byteLength > this.config.maxStateBytes) return;
    }
    let lastError: unknown;
    for (let attempt = 0; attempt <= this.config.storeRetries; attempt += 1) {
      const counter = live.counter + 1;
      try {
        await this.api.putState(live.id, this.lease.token, counter, update, false, live.receipt ?? undefined);
        live.counter = counter;
        live.lastStoredHash = createHash("sha256").update(update).digest("hex");
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
