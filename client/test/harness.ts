/** Fakes for testing RoomClient deterministically: a virtual clock, a scripted socket and fetch. */
import type { Deps, Options, ResponseLike, SocketLike } from "../src/room-client.js";
import { RoomClient } from "../src/room-client.js";
import type { Bootstrap, Message, Member, RoomEvent } from "../src/protocol.js";

export const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

export class FakeClock {
  t = 1_000_000;
  private nextId = 1;
  timers = new Map<number, { at: number; fn: () => void }>();
  now = () => this.t;
  setTimeout = (fn: () => void, ms: number): unknown => {
    const id = this.nextId++;
    this.timers.set(id, { at: this.t + Math.max(0, ms), fn });
    return id;
  };
  clearTimeout = (handle: unknown): void => {
    this.timers.delete(handle as number);
  };
  get pending(): number {
    return this.timers.size;
  }
  /** Advance virtual time, running due timers in order and letting promise chains settle. */
  async advance(ms: number): Promise<void> {
    const target = this.t + ms;
    await flush();
    for (;;) {
      let next: [number, { at: number; fn: () => void }] | undefined;
      for (const entry of this.timers) if (entry[1].at <= target && (!next || entry[1].at < next[1].at)) next = entry;
      if (!next) break;
      this.timers.delete(next[0]);
      this.t = Math.max(this.t, next[1].at);
      next[1].fn();
      await flush();
    }
    this.t = target;
    await flush();
  }
}

export class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, unknown>[] = [];
  closedByClient: number | null = null;
  constructor(public url: string) {}
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(code = 1000): void {
    this.closedByClient = code;
  }
  // ---- the server's side, driven by tests
  open(): void {
    this.onopen?.();
  }
  receive(frame: object): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  serverClose(code: number): void {
    this.onclose?.({ code });
  }
  frames(type: string): Record<string, unknown>[] {
    return this.sent.filter((f) => f.type === type);
  }
}

export type Handler = (url: string, init?: { method?: string; body?: string; signal?: AbortSignal }) => unknown;

export class FakeFetch {
  calls: { url: string; method: string; body?: string; signal?: AbortSignal }[] = [];
  handlers = new Map<string, Handler>();
  /** `path` matches by prefix; the handler returns {status, body} or a Promise of it. */
  on(path: string, handler: Handler): void {
    this.handlers.set(path, handler);
  }
  count(fragment: string): number {
    return this.calls.filter((c) => c.url.includes(fragment)).length;
  }
  fetch = async (
    url: string,
    init?: { method?: string; headers?: Record<string, string>; body?: string; signal?: AbortSignal },
  ): Promise<ResponseLike> => {
    this.calls.push({ url, method: init?.method ?? "GET", body: init?.body, signal: init?.signal });
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    let best: Handler | undefined;
    let bestLength = -1;
    for (const [prefix, handler] of this.handlers) {
      if (path.startsWith(prefix) && prefix.length > bestLength) {
        best = handler;
        bestLength = prefix.length;
      }
    }
    if (!best) throw new Error(`no fake handler for ${path}`);
    const result = (await best(path, init)) as { status?: number; body?: unknown };
    const status = result?.status ?? 200;
    return { status, ok: status >= 200 && status < 300, json: async () => result?.body };
  };
}

export const ROOM = "11111111-1111-1111-1111-111111111111";
export const ME = "22222222-2222-2222-2222-222222222222";
export const OTHER = "33333333-3333-3333-3333-333333333333";

export function message(id: string, version = 1, extra: Partial<Message> = {}): Message {
  return {
    id,
    room_id: ROOM,
    author_id: OTHER,
    author_display_name: "Other",
    client_message_id: `c-${id}`,
    body: `body of ${id} v${version}`,
    created_at: "2026-01-01T00:00:00Z",
    edited_at: null,
    deleted_at: null,
    deleted_by: null,
    version,
    is_deleted: false,
    ...extra,
  };
}

export function tombstone(id: string, version: number): Message {
  return message(id, version, { body: null, is_deleted: true, deleted_at: "2026-01-01T01:00:00Z" });
}

export function member(userId: string, role: Member["role"] = "editor"): Member {
  return { user_id: userId, display_name: userId.slice(0, 4), role, joined_at: "2026-01-01T00:00:00Z" };
}

export function bootstrap(through: number, messages: Message[] = [], members?: Member[]): Bootstrap {
  return {
    room: {
      id: ROOM,
      name: "Room",
      owner_id: ME,
      created_at: "x",
      updated_at: "x",
      active_document_id: null,
      active_document_version: 0,
    },
    role: "owner",
    membership_epoch: 1,
    members: members ?? [member(ME, "owner")],
    messages,
    next_before: null,
    through_seq: through,
  };
}

export function event(seq: number, type: string, resourceId: string | null, extra: Partial<RoomEvent> = {}): RoomEvent {
  const resource = type.startsWith("message_")
    ? "message"
    : type.startsWith("member_")
      ? "membership"
      : type.includes("document")
        ? "document"
        : "room";
  return {
    type: "room_event",
    schema_version: 1,
    event_id: `e-${seq}`,
    room_id: ROOM,
    room_seq: seq,
    event_type: type,
    resource_type: resource,
    resource_id: resourceId,
    resource_version: resource === "message" ? 1 : null,
    payload: {},
    actor_id: OTHER,
    created_at: "x",
    ...extra,
  };
}

export class Rig {
  clock = new FakeClock();
  net = new FakeFetch();
  sockets: FakeSocket[] = [];
  client: RoomClient;
  rand = 0.5;
  boot = bootstrap(10);
  messages = new Map<string, Message>();

  constructor(options: Partial<Options> = {}) {
    const deps: Deps = {
      createSocket: (url) => {
        const socket = new FakeSocket(url);
        this.sockets.push(socket);
        return socket;
      },
      fetch: this.net.fetch,
      setTimeout: this.clock.setTimeout,
      clearTimeout: this.clock.clearTimeout,
      now: this.clock.now,
      random: () => this.rand,
    };
    this.client = new RoomClient(deps, {
      roomId: ROOM,
      userId: ME,
      httpBase: "http://api",
      wsBase: "ws://api",
      ...options,
    });
    this.net.on(`/rooms/${ROOM}/sync`, () => ({ body: this.boot }));
    this.net.on(`/rooms/${ROOM}/messages/`, (path) => {
      const id = path.split("/").pop()!;
      const found = this.messages.get(id);
      return found ? { body: found } : { status: 404, body: { error: { code: "message_not_found" } } };
    });
    this.net.on(`/rooms/${ROOM}/members`, () => ({ body: { items: this.boot.members, next_cursor: null } }));
    this.net.on(`/rooms/${ROOM}`, () => ({ body: { ...this.boot.room, role: "owner" } }));
    this.net.on("/auth/csrf", () => ({ body: { csrf_token: "csrf" } }));
  }

  get socket(): FakeSocket {
    return this.sockets[this.sockets.length - 1]!;
  }

  /** start, bootstrap, open the socket and complete an (empty) sync. */
  async goLive(through = 10): Promise<FakeSocket> {
    this.boot = bootstrap(through, this.boot.messages, this.boot.members);
    this.client.start();
    await flush();
    const socket = this.socket;
    socket.open();
    socket.receive({ type: "sync_started", schema_version: 1, room_id: ROOM, after_seq: through, through_seq: through });
    socket.receive({ type: "sync_complete", through_seq: through });
    await this.clock.advance(0);
    return socket;
  }

  /** Deliver events then let the idle batch timer fire and everything settle. */
  async deliver(...events: RoomEvent[]): Promise<void> {
    for (const e of events) this.socket.receive(e);
    await this.clock.advance(60);
  }
}
