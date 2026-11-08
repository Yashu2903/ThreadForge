/**
 * RoomClient: a small state machine that keeps ONE room's state in sync with the backend.
 *
 * WHERE STATE LIVES. PostgreSQL holds the truth. This object holds a copy in MEMORY: the room, the
 * members, the messages and a CURSOR (the highest room_seq it has fully processed) TOGETHER. They
 * are installed together from a bootstrap and move together; the cursor is never persisted on its
 * own (a stale cursor with fresh state, or the reverse, would silently skip or repeat changes).
 * A page refresh therefore starts from a bootstrap.
 *
 * FLOW  bootstrap (GET /sync) -> install state + cursor=through_seq -> open socket -> send
 *       resume{after_seq: cursor} -> events arrive in ascending room_seq -> sync_complete -> live.
 *
 * EVENTS ARE REFERENCES, NOT DATA. A message_edited event says "message M reached version 3". The
 * client fetches M and merges it by (id, version): a fetched copy replaces the local one only if
 * its version is >= the local version, and a tombstone is final. The cursor advances (and an ack
 * is sent) only AFTER every fetch a batch needed has succeeded, so a crash or disconnect before
 * that point just replays the batch (duplicates are ignored by room_seq).
 *
 * Everything that talks to the outside world is injected (socket factory, fetch, timers, random),
 * so the whole machine is unit-tested with a fake socket and a fake clock.
 */

import {
  type Availability,
  type Bootstrap,
  Close,
  type ClientFrame,
  type Member,
  type Message,
  type RoomEvent,
  type RoomInfo,
  SCHEMA_VERSION,
  type ServerFrame,
  type Status,
  type TerminalReason,
} from "./protocol.js";

// ---- tunables (all in milliseconds unless noted) --------------------------------------------------
export const DEFAULTS = {
  fetchThreshold: 20, // more distinct affected messages than this -> fresh bootstrap instead
  fetchConcurrency: 4,
  batchIdleMs: 50, // events are batched until the socket is idle this long
  batchMaxEvents: 100, // ...or until this many are buffered
  fetchRetries: 3,
  fetchRetryBaseMs: 500,
  heartbeatIntervalMs: 15_000,
  silenceTimeoutMs: 45_000, // no server frame of any kind for this long -> reconnect
  reconnectInitialMs: 1_000,
  reconnectMaxMs: 30_000,
  stableAfterMs: 30_000, // a connection live this long resets the backoff
  tooManyConnectionsDelayMs: 10_000,
  tooManyConnectionsMaxTries: 3,
  slowConsumerWindowMs: 300_000, // 3 x 4429 within this -> resync
  slowConsumerLimit: 3,
  resyncBreakerWindowMs: 60_000,
  resyncBreakerLimit: 3, // MORE than this many resyncs within the window -> terminal
  ackMinIntervalMs: 120, // the server allows 10 acks/second
  typingStartIntervalMs: 3_000,
  typingStopAfterMs: 4_000,
  typingLocalExpiryMs: 6_000,
};
export type Tunables = typeof DEFAULTS;

// ---- injected dependencies ----------------------------------------------------------------------------------
export interface SocketLike {
  send(data: string): void;
  close(code?: number): void;
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
}

export interface ResponseLike {
  status: number;
  ok: boolean;
  json(): Promise<unknown>;
}

export interface Deps {
  createSocket(url: string): SocketLike;
  fetch(
    url: string,
    init?: { method?: string; headers?: Record<string, string>; body?: string; signal?: AbortSignal },
  ): Promise<ResponseLike>;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  now(): number;
  random(): number;
}

export interface Options {
  roomId: string;
  userId: string;
  httpBase: string; // e.g. https://api.example.com
  wsBase: string; // e.g. wss://api.example.com
  tunables?: Partial<Tunables>;
}

export interface State {
  status: Status;
  terminalReason: TerminalReason | null;
  room: RoomInfo | null;
  role: string | null;
  members: Member[];
  messages: Map<string, Message>;
  nextBefore: string | null;
  cursor: number;
  presence: { online: { user_id: string; availability: Availability }[]; degraded: boolean };
  typing: { userIds: string[]; degraded: boolean };
}

type Reaction =
  | { kind: "reconnect" }
  | { kind: "resync" }
  | { kind: "terminal"; reason: TerminalReason; clear: boolean }
  | { kind: "too_many" };

/** The close-code table: what the client does for each code. */
export function reactionFor(code: number): Reaction {
  switch (code) {
    case Close.UNSUPPORTED_DATA:
    case Close.MESSAGE_TOO_BIG:
    case Close.PROTOCOL_VIOLATION:
      return { kind: "terminal", reason: "protocol_error", clear: false };
    case Close.SESSION_INVALID:
      return { kind: "terminal", reason: "sign_in_required", clear: false };
    case Close.ACCESS_REVOKED:
      return { kind: "terminal", reason: "access_lost", clear: true };
    case Close.TOO_MANY_CONNECTIONS:
      return { kind: "too_many" };
    case Close.RESYNC_REQUIRED:
      return { kind: "resync" };
    default: // 1001, 1013, 4408, 4429, 1006 and anything unknown: try again with backoff
      return { kind: "reconnect" };
  }
}

/** A fetch (or the bootstrap) proved the user can no longer use this room. */
class AccessLost extends Error {
  constructor(readonly reason: TerminalReason = "access_lost") {
    super(reason);
  }
}
class FetchFailed extends Error {}

export class RoomClient {
  readonly state: State;
  private readonly t: Tunables;
  private readonly listeners = new Set<() => void>();
  private readonly deps: Deps;
  private readonly opts: Options;

  private generation = 0; // bumped by start/stop/switch: callbacks from older generations are ignored
  private socket: SocketLike | null = null;
  private abort: AbortController | null = null;

  // event pipeline
  private lastQueuedSeq = 0; // highest seq accepted from the socket (>= cursor)
  private buffer: RoomEvent[] = [];
  private processing = false;
  private batchTimer: unknown = null;
  private syncStartedThrough = 0;
  private socketSeq = 0; // highest room_seq the server has sent on the CURRENT socket
  private gotFrame = false; // did the CURRENT socket ever deliver a frame (i.e. was it accepted)?

  // timers
  private heartbeatTimer: unknown = null;
  private silenceTimer: unknown = null;
  private reconnectTimer: unknown = null;
  private stableTimer: unknown = null;
  private ackTimer: unknown = null;
  private lastAckAt = -Infinity;
  private ackedThrough = 0;
  private typingExpiryTimer: unknown = null;
  private typingStopTimer: unknown = null;
  private lastTypingStartAt = -Infinity;
  private typingActive = false;

  // reconnect bookkeeping
  private reconnectAttempts = 0;
  private tooManyTries = 0;
  private slowConsumerAt: number[] = [];
  private resyncAt: number[] = [];

  // unconfirmed sends: client_message_id -> body (retried with the SAME id after a reconnect)
  private readonly pendingSends = new Map<string, string>();

  constructor(deps: Deps, opts: Options) {
    this.deps = deps;
    this.opts = opts;
    this.t = { ...DEFAULTS, ...opts.tunables };
    this.state = RoomClient.emptyState("bootstrapping");
  }

  private static emptyState(status: Status): State {
    return {
      status,
      terminalReason: null,
      room: null,
      role: null,
      members: [],
      messages: new Map(),
      nextBefore: null,
      cursor: 0,
      presence: { online: [], degraded: false },
      typing: { userIds: [], degraded: false },
    };
  }

  // ---- public API --------------------------------------------------------------------------------------

  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Begin (or restart) synchronization from a bootstrap. */
  start(): void {
    this.teardown();
    const gen = ++this.generation;
    this.abort = new AbortController();
    this.state.terminalReason = null;
    this.state.status = "bootstrapping";
    this.reconnectAttempts = 0;
    this.emit();
    void this.bootstrapThenConnect(gen);
  }

  /** Stop everything: socket, timers and fetches. Room state is kept unless `clear` is set. */
  stop(clear = false): void {
    this.generation++;
    this.teardown();
    if (clear) this.clearRoomState();
    this.state.status = "terminal";
    this.state.terminalReason = "stopped";
    this.emit();
  }

  /** Switch rooms: the old room's socket, timers and fetches are cancelled and its state dropped. */
  switchRoom(roomId: string): void {
    this.generation++;
    this.teardown();
    this.clearRoomState();
    (this.opts as { roomId: string }).roomId = roomId;
    this.lastQueuedSeq = 0;
    this.buffer = [];
    this.pendingSends.clear();
    this.start();
  }

  /** The user pressed a key. Sends `typing start` at most every 3 s and `stop` after 4 s idle. */
  userTyping(): void {
    if (this.state.status !== "live") return;
    const now = this.deps.now();
    if (!this.typingActive || now - this.lastTypingStartAt >= this.t.typingStartIntervalMs) {
      this.sendFrame({ type: "typing", state: "start" });
      this.lastTypingStartAt = now;
      this.typingActive = true;
    }
    if (this.typingStopTimer) this.deps.clearTimeout(this.typingStopTimer);
    this.typingStopTimer = this.deps.setTimeout(() => this.stopTyping(), this.t.typingStopAfterMs);
  }

  /** Call when a message was sent or the input was cleared. */
  stopTyping(): void {
    if (this.typingStopTimer) this.deps.clearTimeout(this.typingStopTimer);
    this.typingStopTimer = null;
    if (this.typingActive) {
      this.typingActive = false;
      this.lastTypingStartAt = -Infinity;
      this.sendFrame({ type: "typing", state: "stop" });
    }
  }

  setAvailability(value: Availability): void {
    this.sendFrame({ type: "set_availability", value });
  }

  /**
   * Send a message over HTTP. The id is kept until the server confirms it, so a retry after a
   * reconnect re-sends the SAME client_message_id (the backend makes that safe: 200 with the
   * existing message instead of a duplicate). Writes never go over the socket.
   */
  async sendMessage(body: string, clientMessageId: string = crypto.randomUUID()): Promise<Message | null> {
    this.pendingSends.set(clientMessageId, body);
    this.stopTyping();
    return this.postPending(clientMessageId);
  }

  /** Re-send every unconfirmed message with its original id (after a reconnect). */
  async retryPending(): Promise<void> {
    for (const id of [...this.pendingSends.keys()]) await this.postPending(id);
  }

  get pendingSendIds(): string[] {
    return [...this.pendingSends.keys()];
  }

  // ---- bootstrap -----------------------------------------------------------------------------------------

  private async bootstrapThenConnect(gen: number): Promise<void> {
    try {
      const boot = await this.fetchBootstrap(gen);
      if (gen !== this.generation) return;
      this.installBootstrap(boot);
      this.connect(gen);
    } catch (err) {
      if (gen !== this.generation) return;
      if (err instanceof AccessLost) return this.accessLost(err);
      this.scheduleReconnect(gen, true); // transient: bootstrap again after a backoff
    }
  }

  private async fetchBootstrap(gen: number): Promise<Bootstrap> {
    const res = await this.get(`/rooms/${this.opts.roomId}/sync`, gen);
    return (await res.json()) as Bootstrap;
  }

  /** State and cursor are installed in the SAME step. */
  private installBootstrap(boot: Bootstrap): void {
    const s = this.state;
    s.room = boot.room;
    s.role = boot.role;
    s.members = boot.members;
    s.messages = new Map(boot.messages.map((m) => [m.id, m]));
    s.nextBefore = boot.next_before;
    s.cursor = boot.through_seq;
    // frames already buffered beyond the bootstrap stay (they continue it); the rest is covered
    this.buffer = this.buffer.filter((e) => e.room_seq > boot.through_seq);
    this.lastQueuedSeq = Math.max(boot.through_seq, this.buffer.at(-1)?.room_seq ?? 0);
    this.emit();
  }

  // ---- the socket -----------------------------------------------------------------------------------------

  private connect(gen: number): void {
    if (gen !== this.generation) return;
    this.closeSocket();
    this.state.status = "syncing";
    this.lastQueuedSeq = this.state.cursor;
    this.gotFrame = false;
    this.socketSeq = this.state.cursor; // the highest seq the SERVER has sent on this socket
    this.ackedThrough = this.state.cursor; // the server starts counting acks from the resume point
    this.buffer = [];
    this.emit();
    const socket = this.deps.createSocket(`${this.opts.wsBase}/rooms/${this.opts.roomId}/ws`);
    this.socket = socket;
    socket.onopen = () => {
      if (gen !== this.generation || this.socket !== socket) return;
      this.sendFrame({ type: "resume", schema_version: SCHEMA_VERSION, after_seq: this.state.cursor });
      this.armSilence(gen);
      this.armHeartbeat(gen);
    };
    socket.onmessage = (event) => {
      if (gen !== this.generation || this.socket !== socket) return;
      this.onFrame(gen, event.data);
    };
    socket.onclose = (event) => {
      if (gen !== this.generation || this.socket !== socket) return;
      this.socket = null;
      this.onClosed(gen, event.code);
    };
    socket.onerror = () => {
      /* a close always follows; nothing to do here */
    };
  }

  private closeSocket(): void {
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      try {
        socket.close(1000);
      } catch {
        /* already closed */
      }
    }
    this.clearTimer("heartbeatTimer");
    this.clearTimer("silenceTimer");
    this.clearTimer("stableTimer");
    this.clearTimer("batchTimer");
    this.clearTimer("ackTimer");
  }

  private onClosed(gen: number, code: number): void {
    const accepted = this.gotFrame; // true: the socket WAS accepted, so this is a plain drop
    this.closeSocket();
    const reaction = reactionFor(code);
    const now = this.deps.now();
    switch (reaction.kind) {
      case "terminal":
        return this.terminal(reaction.reason, reaction.clear);
      case "resync":
        return void this.resync(gen);
      case "too_many":
        this.tooManyTries++;
        if (this.tooManyTries >= this.t.tooManyConnectionsMaxTries) return this.terminal("too_many_connections", false);
        return this.scheduleReconnect(gen, false, this.t.tooManyConnectionsDelayMs);
      case "reconnect":
        if (code === Close.SLOW_CONSUMER) {
          this.slowConsumerAt = this.slowConsumerAt.filter((at) => now - at < this.t.slowConsumerWindowMs);
          this.slowConsumerAt.push(now);
          if (this.slowConsumerAt.length >= this.t.slowConsumerLimit) {
            this.slowConsumerAt = [];
            return void this.resync(gen); // repeatedly too slow: start over from a bootstrap
          }
        }
        // A handshake the server REFUSES (removed member, revoked or expired session) looks like
        // an abnormal close (1006) with no frames: browsers cannot read the HTTP status. Ask over
        // HTTP instead: the bootstrap answers 401/403/404 definitively, so access loss is found.
        return this.scheduleReconnect(gen, code === 1006 && !accepted);
    }
  }

  // ---- incoming frames ------------------------------------------------------------------------------------

  private onFrame(gen: number, raw: string): void {
    this.armSilence(gen); // ANY server frame proves the connection is alive
    this.gotFrame = true;
    let frame: ServerFrame;
    try {
      frame = JSON.parse(raw) as ServerFrame;
    } catch {
      return; // not ours to interpret; the server never sends non-JSON
    }
    switch (frame.type) {
      case "sync_started":
        this.syncStartedThrough = frame.through_seq;
        break;
      case "room_event":
        this.onEvent(gen, frame);
        break;
      case "sync_complete":
        this.clearTimer("batchTimer");
        void this.processBatch(gen, true);
        break;
      case "presence_snapshot":
        this.state.presence = { online: frame.online, degraded: frame.degraded };
        this.emit();
        break;
      case "typing_state":
        this.state.typing = { userIds: frame.typing, degraded: frame.degraded };
        this.armTypingExpiry(gen);
        this.emit();
        break;
      case "resync_required":
        void this.resync(gen);
        break;
      case "heartbeat_ack":
        break;
    }
  }

  private onEvent(gen: number, event: RoomEvent): void {
    if (event.room_seq <= this.lastQueuedSeq) {
      // a duplicate (or already covered by a bootstrap): ignore it, but the server DID send it,
      // so it still counts toward what we may acknowledge
      this.socketSeq = Math.max(this.socketSeq, event.room_seq);
      this.scheduleAck(gen);
      return;
    }
    if (event.room_seq !== this.lastQueuedSeq + 1) {
      // a GAP: never apply past it. Keep the cursor and reconnect from the last processed event.
      this.closeSocket();
      this.scheduleReconnect(gen, false, 0);
      return;
    }
    this.lastQueuedSeq = event.room_seq;
    this.socketSeq = Math.max(this.socketSeq, event.room_seq);
    this.buffer.push(event);
    // Batches run DURING replay too: the server pauses at ACK_WINDOW unacknowledged events, and
    // acks are only sent for processed events, so waiting for sync_complete would deadlock.
    this.clearTimer("batchTimer");
    if (this.buffer.length >= this.t.batchMaxEvents) void this.processBatch(gen, false);
    else this.batchTimer = this.deps.setTimeout(() => void this.processBatch(gen, false), this.t.batchIdleMs);
  }

  // ---- processing a batch -----------------------------------------------------------------------------------

  private async processBatch(gen: number, completesSync: boolean): Promise<void> {
    if (gen !== this.generation) return;
    if (this.processing) {
      if (completesSync) this.pendingSyncComplete = true;
      return;
    }
    this.processing = true;
    try {
      while (this.buffer.length > 0 && gen === this.generation) {
        const batch = this.buffer;
        this.buffer = [];
        await this.applyBatch(gen, batch);
        if (gen !== this.generation) return;
      }
      if ((completesSync || this.pendingSyncComplete) && gen === this.generation) {
        this.pendingSyncComplete = false;
        this.becomeLive(gen);
      }
    } catch (err) {
      if (gen !== this.generation) return;
      if (err instanceof AccessLost) return this.accessLost(err);
      // transient failure after the retries: keep the cursor, reconnect, replay the batch
      this.closeSocket();
      this.scheduleReconnect(gen, false, 0);
    } finally {
      if (gen === this.generation) this.processing = false; // never clear a NEWER generation's flag
    }
  }

  private pendingSyncComplete = false;

  private async applyBatch(gen: number, events: RoomEvent[]): Promise<void> {
    // after a reconnect an older batch may still be in flight: never reprocess or go backwards
    const batch = events.filter((e) => e.room_seq > this.state.cursor);
    if (batch.length === 0) return;
    const last = batch[batch.length - 1]!.room_seq;
    const messageIds = new Set<string>();
    let roomDirty = false;
    let membersDirty = false;
    for (const e of batch) {
      if (e.event_type === "member_removed" && (e.payload as { user_id?: string }).user_id === this.opts.userId) {
        return this.terminal("access_lost", true); // I was removed: stop everything, clear
      }
      if (e.resource_type === "message" && e.resource_id) messageIds.add(e.resource_id);
      if (e.event_type === "room_renamed") roomDirty = true;
      if (e.event_type.startsWith("member_")) membersDirty = true;
    }

    if (messageIds.size > this.t.fetchThreshold) {
      const boot = await this.withRetries(gen, () => this.fetchBootstrap(gen));
      if (gen !== this.generation) return;
      this.installBootstrap(boot); // state and cursor together; the cursor is >= `last`
      this.state.cursor = Math.max(this.state.cursor, last);
    } else {
      await this.fetchMessages(gen, [...messageIds]);
      if (membersDirty) {
        const members = await this.withRetries(gen, () => this.fetchAllMembers(gen));
        if (gen !== this.generation) return;
        this.state.members = members;
      }
      if (roomDirty) {
        const room = await this.withRetries(gen, async () => {
          const res = await this.get(`/rooms/${this.opts.roomId}`, gen);
          return (await res.json()) as RoomInfo & { role?: string };
        });
        if (gen !== this.generation) return;
        this.state.room = {
          id: room.id,
          name: room.name,
          owner_id: room.owner_id,
          created_at: room.created_at,
          updated_at: room.updated_at,
        };
      }
      this.state.cursor = Math.max(this.state.cursor, last); // ONLY now: all fetches succeeded
    }
    this.lastQueuedSeq = Math.max(this.lastQueuedSeq, this.state.cursor);
    this.emit();
    this.scheduleAck(gen);
  }

  private async fetchMessages(gen: number, ids: string[]): Promise<void> {
    let next = 0;
    const worker = async () => {
      while (next < ids.length) {
        const id = ids[next++]!;
        const message = await this.withRetries(gen, async () => {
          try {
            const res = await this.get(`/rooms/${this.opts.roomId}/messages/${id}`, gen);
            return (await res.json()) as Message;
          } catch (err) {
            if (err instanceof FetchFailed && (err as FetchFailed & { code?: string }).code === "message_not_found") {
              return null; // nothing to merge
            }
            throw err;
          }
        });
        if (gen !== this.generation) return;
        if (message) this.mergeMessage(message);
      }
    };
    await Promise.all(Array.from({ length: Math.min(this.t.fetchConcurrency, ids.length) }, worker));
  }

  /** Merge by (id, version). A fetched copy wins only if its version >= local; a tombstone is final. */
  mergeMessage(fetched: Message): void {
    const local = this.state.messages.get(fetched.id);
    if (local) {
      if (local.is_deleted) return; // a tombstone is final: stale live content can never replace it
      if (fetched.version < local.version) return;
    }
    this.state.messages.set(fetched.id, fetched);
  }

  private async fetchAllMembers(gen: number): Promise<Member[]> {
    const out: Member[] = [];
    let cursor: string | null = null;
    do {
      const query: string = cursor ? `?limit=100&cursor=${encodeURIComponent(cursor)}` : "?limit=100";
      const res = await this.get(`/rooms/${this.opts.roomId}/members${query}`, gen);
      const page = (await res.json()) as { items: Member[]; next_cursor: string | null };
      out.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor);
    return out;
  }

  // ---- HTTP helpers -------------------------------------------------------------------------------------------

  private async get(path: string, gen: number): Promise<ResponseLike> {
    if (gen !== this.generation) throw new FetchFailed("stale");
    const res = await this.deps.fetch(this.opts.httpBase + path, { signal: this.abort?.signal });
    if (res.status === 401) throw new AccessLost("sign_in_required"); // the session is gone
    if (res.status === 403) throw new AccessLost();
    if (!res.ok) {
      let code: string | undefined;
      try {
        code = ((await res.json()) as { error?: { code?: string } }).error?.code;
      } catch {
        /* no body */
      }
      if (res.status === 404 && code !== "message_not_found") throw new AccessLost(); // room_not_found
      const err = new FetchFailed(`HTTP ${res.status}`) as FetchFailed & { code?: string };
      err.code = code;
      throw err;
    }
    return res;
  }

  private async withRetries<T>(gen: number, fn: () => Promise<T>): Promise<T> {
    let attempt = 0;
    for (;;) {
      try {
        return await fn();
      } catch (err) {
        if (err instanceof AccessLost || gen !== this.generation) throw err;
        if ((err as FetchFailed & { code?: string }).code === "message_not_found") throw err;
        if (attempt >= this.t.fetchRetries) throw err;
        attempt++;
        await this.sleep(gen, this.t.fetchRetryBaseMs * 2 ** (attempt - 1));
      }
    }
  }

  private sleep(gen: number, ms: number): Promise<void> {
    return new Promise((resolve, reject) => {
      this.deps.setTimeout(() => (gen === this.generation ? resolve() : reject(new FetchFailed("stale"))), ms);
    });
  }

  private async postPending(clientMessageId: string): Promise<Message | null> {
    const body = this.pendingSends.get(clientMessageId);
    if (body === undefined) return null;
    try {
      const csrf = (await (await this.deps.fetch(`${this.opts.httpBase}/auth/csrf`)).json()) as { csrf_token: string };
      const res = await this.deps.fetch(`${this.opts.httpBase}/rooms/${this.opts.roomId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf.csrf_token },
        body: JSON.stringify({ client_message_id: clientMessageId, body }),
      });
      if (res.status === 200 || res.status === 201) {
        this.pendingSends.delete(clientMessageId); // confirmed: never re-sent
        const message = (await res.json()) as Message;
        this.mergeMessage(message);
        this.emit();
        return message;
      }
      if (res.status === 401 || res.status === 403 || res.status === 404) {
        this.pendingSends.delete(clientMessageId);
      }
    } catch {
      /* unconfirmed: stays pending and is retried with the same id */
    }
    return null;
  }

  // ---- going live, acks -------------------------------------------------------------------------------------------

  private becomeLive(gen: number): void {
    this.state.status = "live";
    this.tooManyTries = 0;
    this.clearTimer("stableTimer");
    this.stableTimer = this.deps.setTimeout(() => {
      if (gen === this.generation) this.reconnectAttempts = 0; // stable: the backoff starts over
    }, this.t.stableAfterMs);
    this.scheduleAck(gen);
    this.emit();
    void this.retryPending();
  }

  /** Acks are batched (the server accepts 10/second) and sent only for processed events. */
  private scheduleAck(gen: number): void {
    // Never acknowledge more than the server has SENT (a bootstrap can push the cursor further):
    // the server closes 4400 for an ack above its last_sent.
    if (this.ackTimer || Math.min(this.state.cursor, this.socketSeq) <= this.ackedThrough) return;
    const wait = Math.max(0, this.lastAckAt + this.t.ackMinIntervalMs - this.deps.now());
    this.ackTimer = this.deps.setTimeout(() => {
      this.ackTimer = null;
      const through = Math.min(this.state.cursor, this.socketSeq);
      if (gen !== this.generation || through <= this.ackedThrough) return;
      this.ackedThrough = through;
      this.lastAckAt = this.deps.now();
      this.sendFrame({ type: "ack", through_seq: through });
    }, wait);
  }

  // ---- heartbeat and silence ---------------------------------------------------------------------------------------

  private armHeartbeat(gen: number): void {
    this.clearTimer("heartbeatTimer");
    this.heartbeatTimer = this.deps.setTimeout(() => {
      if (gen !== this.generation) return;
      this.sendFrame({ type: "heartbeat" });
      this.armHeartbeat(gen);
    }, this.t.heartbeatIntervalMs);
  }

  private armSilence(gen: number): void {
    this.clearTimer("silenceTimer");
    this.silenceTimer = this.deps.setTimeout(() => {
      if (gen !== this.generation) return;
      // nothing from the server (not even a heartbeat ack) for too long: the connection is dead
      this.closeSocket();
      this.scheduleReconnect(gen, false, 0);
    }, this.t.silenceTimeoutMs);
  }

  private armTypingExpiry(gen: number): void {
    this.clearTimer("typingExpiryTimer");
    if (this.state.typing.userIds.length === 0) return;
    this.typingExpiryTimer = this.deps.setTimeout(() => {
      if (gen !== this.generation) return;
      this.state.typing = { userIds: [], degraded: this.state.typing.degraded };
      this.emit();
    }, this.t.typingLocalExpiryMs);
  }

  // ---- reconnect and resync -----------------------------------------------------------------------------------------

  /** ONE reconnect loop per client: a pending timer makes further requests no-ops. */
  private scheduleReconnect(gen: number, rebootstrap: boolean, forcedDelayMs?: number): void {
    if (gen !== this.generation || this.reconnectTimer) return;
    this.state.status = "reconnecting";
    this.emit();
    const attempt = this.reconnectAttempts++;
    const delay =
      forcedDelayMs ??
      Math.min(this.t.reconnectMaxMs, this.t.reconnectInitialMs * 2 ** attempt) * (0.5 + this.deps.random() / 2);
    this.reconnectTimer = this.deps.setTimeout(() => {
      this.reconnectTimer = null;
      if (gen !== this.generation) return;
      if (rebootstrap) void this.bootstrapThenConnect(gen);
      else this.connect(gen); // temporary disconnects keep the cursor and the local state
    }, delay);
  }

  /** Stop applying the old stream, fetch a fresh bootstrap, replace state + cursor together. */
  private async resync(gen: number): Promise<void> {
    if (gen !== this.generation) return;
    const now = this.deps.now();
    this.resyncAt = this.resyncAt.filter((at) => now - at < this.t.resyncBreakerWindowMs);
    this.resyncAt.push(now);
    if (this.resyncAt.length > this.t.resyncBreakerLimit) return this.terminal("cannot_synchronize", false);
    this.closeSocket();
    this.state.status = "resyncing";
    this.buffer = [];
    this.emit();
    try {
      const boot = await this.withRetries(gen, () => this.fetchBootstrap(gen));
      if (gen !== this.generation) return;
      this.installBootstrap(boot);
      this.connect(gen);
    } catch (err) {
      if (gen !== this.generation) return;
      if (err instanceof AccessLost) return this.accessLost(err);
      this.scheduleReconnect(gen, true);
    }
  }

  // ---- terminal ---------------------------------------------------------------------------------------------------

  private accessLost(err: AccessLost): void {
    this.terminal(err.reason, err.reason === "access_lost"); // sign-in: keep the data, ask to sign in
  }

  private terminal(reason: TerminalReason, clear: boolean): void {
    this.generation++; // everything in flight is now obsolete
    this.teardown();
    if (clear) this.clearRoomState();
    this.state.status = "terminal";
    this.state.terminalReason = reason;
    this.emit();
  }

  private clearRoomState(): void {
    const s = this.state;
    s.room = null;
    s.role = null;
    s.members = [];
    s.messages = new Map();
    s.nextBefore = null;
    s.cursor = 0;
    s.presence = { online: [], degraded: false };
    s.typing = { userIds: [], degraded: false };
    this.buffer = [];
  }

  /** Close the socket, cancel every timer and abort every fetch. */
  private teardown(): void {
    this.closeSocket();
    for (const name of ["reconnectTimer", "typingExpiryTimer", "typingStopTimer"] as const) this.clearTimer(name);
    this.abort?.abort();
    this.abort = null;
    this.processing = false;
    this.pendingSyncComplete = false;
    this.typingActive = false;
  }

  // ---- small helpers ----------------------------------------------------------------------------------------------------

  private sendFrame(frame: ClientFrame): void {
    try {
      this.socket?.send(JSON.stringify(frame));
    } catch {
      /* the close handler will deal with a dead socket */
    }
  }

  private clearTimer(
    name:
      | "heartbeatTimer"
      | "silenceTimer"
      | "reconnectTimer"
      | "stableTimer"
      | "batchTimer"
      | "ackTimer"
      | "typingExpiryTimer"
      | "typingStopTimer",
  ): void {
    const handle = this[name];
    if (handle) this.deps.clearTimeout(handle);
    this[name] = null;
  }

  private emit(): void {
    for (const listener of [...this.listeners]) listener();
  }
}
