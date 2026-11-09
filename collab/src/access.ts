/**
 * Access over time: an open document connection must stop working when the person's right to it ends.
 *
 * A WebSocket is authenticated ONCE, when it opens. Without this class a removed member, a signed-out
 * user or a demoted editor would keep a working socket (reading and, for a demoted editor, writing)
 * for as long as the tab stays open. Three triggers make a CHECK happen:
 *
 *   1. a timer, every `accessCheckMs` (default 15 s)  - always runs, needs nothing else;
 *   2. a Redis room hint (`{prefix}:room:{room_id}`, published by the outbox dispatcher whenever a room
 *      event such as member_removed / member_role_changed is committed) - makes the check happen
 *      within about a second instead of up to 15 s. At most one check per room per second.
 *      Redis is only a HINT: if it is down the timer alone keeps enforcing access;
 *   3. nothing else: no push from the API is needed.
 *
 * A check asks the API ONE question per room ("POST /internal/collab/access/check": two SQL statements
 * per room however many connections it has) and applies the answer to each connection:
 *
 *   session_invalid          close 4401 (the client keeps its text and does not reconnect)
 *   not_member / doc gone    close 4403 (the same)
 *   still a member, now a
 *   viewer                   the connection becomes READ-ONLY at runtime (Hocuspocus drops a read-only
 *                            connection's updates itself) and the client is told, so its editor locks.
 *                            A downgrade is never silently ignored.
 *   API / database failing   keep serving for up to `accessGraceMs` (30 s: a blip must not disconnect
 *                            everyone), then close with 1013 "try again later" so nobody keeps editing
 *                            on access that can no longer be verified.
 */
import { createClient } from "redis";
import type { Connection } from "@hocuspocus/server";
import { ApiError, type InternalApi } from "./api.js";
import type { Logger } from "./log.js";

/** The parts of the server this class needs (kept narrow so tests can drive it). */
export interface AccessHost {
  /** every open document connection */
  connections(): Connection[];
  close(connection: Connection, code: number, reason: string): void;
  downgrade(connection: Connection): void;
}

export interface AccessOptions {
  accessCheckMs: number;
  accessGraceMs: number;
  redisUrl: string;
  redisPrefix: string;
  /** minimum gap between two hint-triggered checks of one room */
  hintMinGapMs?: number;
}

export const ACCESS_CLOSE = { SESSION_INVALID: 4401, NOT_A_MEMBER: 4403, UNAVAILABLE: 1013 } as const;

interface Ctx {
  sessionId: string;
  documentId: string;
  roomId: string;
  canEdit: boolean;
}

export class AccessMonitor {
  private timer: NodeJS.Timeout | null = null;
  private lastOkAt = Date.now();
  private running: Promise<void> | null = null;
  private rerun: Set<string> | "all" | null = null;
  private readonly lastHintRun = new Map<string, number>();
  private readonly pendingHint = new Map<string, NodeJS.Timeout>();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private redis: any = null;
  private readonly subscribed = new Set<string>();
  private readonly minGap: number;
  private stopped = false;
  /** how many checks ran and why, for tests and /health */
  readonly stats = { periodic: 0, hint: 0, failures: 0, redisReady: false };

  constructor(
    private readonly api: InternalApi,
    private readonly host: AccessHost,
    private readonly log: Logger,
    private readonly options: AccessOptions,
  ) {
    this.minGap = options.hintMinGapMs ?? 1000;
  }

  start(): void {
    this.lastOkAt = Date.now();
    this.timer = setInterval(() => void this.check("all", "periodic"), this.options.accessCheckMs);
    this.timer.unref();
    if (this.options.redisUrl) this.connectRedis();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    for (const t of this.pendingHint.values()) clearTimeout(t);
    this.pendingHint.clear();
    const client = this.redis;
    this.redis = null;
    if (client) {
      // A client that never connected would wait forever on quit(): only ask nicely when it is up.
      if (client.isReady) {
        await Promise.race([client.quit().catch(() => undefined), new Promise((r) => setTimeout(r, 1500))]);
      }
      client.destroy();
    }
    await this.running?.catch(() => undefined);
  }

  // ---- Redis hints ---------------------------------------------------------------------------

  private connectRedis(): void {
    const client = createClient({
      url: this.options.redisUrl,
      socket: { reconnectStrategy: (retries) => Math.min(250 * 2 ** retries, 10_000) },
    });
    let lastLogged = 0;
    client.on("error", (error: Error) => {
      this.stats.redisReady = false;
      if (Date.now() - lastLogged > 30_000) {
        lastLogged = Date.now();
        // the periodic check keeps enforcing access without Redis
        this.log.warn("redis unavailable; access is enforced by the timer only", { error: error.name });
      }
    });
    client.on("ready", () => {
      this.stats.redisReady = true;
      this.subscribed.clear(); // a new connection has no subscriptions
      void this.syncSubscriptions();
    });
    this.redis = client;
    client.connect().catch(() => undefined); // reconnects are handled by reconnectStrategy
  }

  /** Subscribe to the rooms that have connections here, unsubscribe from the rest. */
  async syncSubscriptions(): Promise<void> {
    const client = this.redis;
    if (!client || !client.isReady || this.stopped) return;
    const wanted = new Set(this.host.connections().map((c) => (c.context as Ctx).roomId));
    for (const room of wanted) {
      if (this.subscribed.has(room)) continue;
      this.subscribed.add(room);
      try {
        await client.subscribe(`${this.options.redisPrefix}:room:${room}`, () => this.hint(room));
      } catch {
        this.subscribed.delete(room);
      }
    }
    for (const room of [...this.subscribed]) {
      if (wanted.has(room)) continue;
      this.subscribed.delete(room);
      try {
        await client.unsubscribe(`${this.options.redisPrefix}:room:${room}`);
      } catch {
        /* the next sync retries */
      }
    }
  }

  /** A room event was committed: check that room soon, but at most once per second. */
  hint(roomId: string): void {
    if (this.stopped || this.pendingHint.has(roomId)) return;
    const wait = (this.lastHintRun.get(roomId) ?? 0) + this.minGap - Date.now();
    if (wait <= 0) {
      this.lastHintRun.set(roomId, Date.now());
      void this.check(new Set([roomId]), "hint");
      return;
    }
    this.pendingHint.set(
      roomId,
      setTimeout(() => {
        this.pendingHint.delete(roomId);
        this.lastHintRun.set(roomId, Date.now());
        void this.check(new Set([roomId]), "hint");
      }, wait),
    );
  }

  // ---- the check -----------------------------------------------------------------------------

  /** Run one check for these rooms (or all). Overlapping requests are merged into one rerun. */
  check(rooms: Set<string> | "all", why: "periodic" | "hint"): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.running) {
      this.rerun = this.rerun === "all" || rooms === "all" ? "all" : new Set([...(this.rerun ?? []), ...rooms]);
      return this.running;
    }
    this.running = this.run(rooms, why).finally(() => {
      this.running = null;
      const next = this.rerun;
      this.rerun = null;
      if (next && !this.stopped) void this.check(next, why);
    });
    return this.running;
  }

  private async run(rooms: Set<string> | "all", why: "periodic" | "hint"): Promise<void> {
    if (why === "periodic") this.stats.periodic += 1;
    else this.stats.hint += 1;
    const byRoom = new Map<string, Connection[]>();
    for (const connection of this.host.connections()) {
      const ctx = connection.context as Ctx;
      if (rooms !== "all" && !rooms.has(ctx.roomId)) continue;
      const list = byRoom.get(ctx.roomId) ?? [];
      list.push(connection);
      byRoom.set(ctx.roomId, list);
    }
    if (byRoom.size === 0) {
      this.lastOkAt = Date.now(); // nothing to protect, nothing failing
      return;
    }
    let answer;
    try {
      answer = await this.api.checkAccess(
        [...byRoom].map(([roomId, list]) => ({
          room_id: roomId,
          connections: list.map((c) => ({ session_id: (c.context as Ctx).sessionId, document_id: (c.context as Ctx).documentId })),
        })),
      );
    } catch (error) {
      this.stats.failures += 1;
      this.log.warn("access check failed", { code: error instanceof ApiError ? error.code : "error" });
      if (Date.now() - this.lastOkAt > this.options.accessGraceMs) {
        // Access can no longer be verified. Stop serving rather than serve on stale permission.
        for (const list of byRoom.values()) {
          for (const connection of list) this.host.close(connection, ACCESS_CLOSE.UNAVAILABLE, "access_unverifiable");
        }
      }
      return;
    }
    this.lastOkAt = Date.now();
    const byId = new Map(answer.rooms.map((r) => [r.room_id, r.results]));
    for (const [roomId, list] of byRoom) {
      const results = byId.get(roomId);
      if (!results || results.length !== list.length) continue; // malformed answer: change nothing
      list.forEach((connection, index) => this.apply(connection, results[index]!));
    }
  }

  private apply(connection: Connection, result: { status: string; can_edit: boolean }): void {
    const ctx = connection.context as Ctx;
    switch (result.status) {
      case "ok":
        if (!result.can_edit && !connection.readOnly) {
          this.log.info("connection downgraded to read-only", { document: ctx.documentId });
          this.host.downgrade(connection);
        }
        return;
      case "session_invalid":
        this.host.close(connection, ACCESS_CLOSE.SESSION_INVALID, "session_invalid");
        return;
      case "not_member":
      case "document_gone":
        this.host.close(connection, ACCESS_CLOSE.NOT_A_MEMBER, "not_a_member");
        return;
      default:
        this.log.warn("unknown access status", { status: result.status });
    }
  }
}
