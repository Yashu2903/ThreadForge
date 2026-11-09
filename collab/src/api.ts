/**
 * Client for the FastAPI /internal/collab API. This is the ONLY way the collaboration service reaches
 * PostgreSQL. Every call carries the shared secret; writes also carry the lease token.
 * Errors carry a status and the API's stable error `code`, never a response body.
 */
import type { PMJson } from "@threadforge/editor-schema";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly extra: Record<string, unknown> = {},
  ) {
    super(`internal api ${status} ${code}`);
    this.name = "ApiError";
  }
  /** The API (or database) is unreachable or failing: retry, do not treat as a verdict. */
  get transient(): boolean {
    return this.status === 0 || this.status >= 500 || this.status === 429;
  }
}

export type CollabState = "legacy" | "ready" | "needs_repair";

export interface Access {
  user_id: string;
  display_name: string;
  session_id: string;
  role: string;
  can_edit: boolean;
  room_id: string;
  document_id: string;
  collab_state: CollabState;
}
export interface AccessRoomQuery {
  room_id: string;
  connections: { session_id: string; document_id: string }[];
}
export interface AccessRoomAnswer {
  room_id: string;
  results: { status: "ok" | "session_invalid" | "not_member" | "document_gone"; can_edit: boolean }[];
}
export interface WriteDecision {
  allowed: boolean;
  reason: string;
  role_ok: boolean;
  role: string | null;
  collab_state: CollabState | null;
  room_id: string | null;
}
export interface LeaseInfo {
  token: number;
  expires_at: string;
  ttl_seconds: number;
}
export interface StoredState {
  state: Uint8Array;
  /** -1 = the document has no Yjs state yet */
  counter: number;
  collabState: CollabState;
  roomId: string;
}
export interface Seed {
  format: "interchange" | "tiptap";
  content: unknown;
  room_id: string;
}
export interface SnapshotIn {
  kind: "seed" | "periodic" | "final";
  yjs_update_counter: number;
  content: PMJson;
  content_hash: string;
  text_length: number;
}

interface CallInit {
  json?: unknown;
  body?: Uint8Array;
  headers?: Record<string, string>;
}

export class InternalApi {
  constructor(
    private readonly baseUrl: string,
    private readonly secret: string,
    private readonly timeoutMs: number,
  ) {}

  private async call(method: string, path: string, init: CallInit = {}): Promise<Response> {
    const headers: Record<string, string> = { "X-Internal-Secret": this.secret, ...init.headers };
    let body: string | Uint8Array | undefined;
    if (init.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(init.json);
    } else if (init.body) {
      headers["Content-Type"] = "application/octet-stream";
      body = init.body;
    }
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/internal/collab${path}`, {
        method,
        headers,
        body: body as never,
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      throw new ApiError(0, "unreachable");
    }
    if (response.ok) return response;
    let code = "http_error";
    let extra: Record<string, unknown> = {};
    try {
      const parsed = (await response.json()) as { error?: Record<string, unknown> };
      const { code: c, message: _message, request_id: _requestId, ...rest } = parsed.error ?? {};
      if (typeof c === "string") code = c;
      extra = rest;
    } catch {
      /* not JSON (a proxy error page): keep the generic code */
    }
    throw new ApiError(response.status, code, extra);
  }

  async acquireLease(holderId: string): Promise<LeaseInfo> {
    const r = await this.call("POST", "/lease/acquire", { json: { holder_id: holderId } });
    return (await r.json()) as LeaseInfo;
  }
  async renewLease(holderId: string, token: number): Promise<LeaseInfo> {
    const r = await this.call("POST", "/lease/renew", { json: { holder_id: holderId, token } });
    return (await r.json()) as LeaseInfo;
  }
  async releaseLease(holderId: string, token: number): Promise<void> {
    await this.call("POST", "/lease/release", { json: { holder_id: holderId, token } });
  }

  async authorize(cookieHeader: string | null, documentId: string): Promise<Access> {
    const r = await this.call("POST", "/authorize", { json: { cookie_header: cookieHeader, document_id: documentId } });
    return (await r.json()) as Access;
  }

  /** May this USER (by id, not by cookie) write to this document now? Same rule as the editor's. */
  async canWrite(userId: string, documentId: string): Promise<WriteDecision> {
    const r = await this.call("POST", "/can-write", { json: { user_id: userId, document_id: documentId } });
    return (await r.json()) as WriteDecision;
  }

  /** One question for every open connection of the given rooms (see services/collab.check_access_batch). */
  async checkAccess(rooms: AccessRoomQuery[]): Promise<{ rooms: AccessRoomAnswer[] }> {
    const r = await this.call("POST", "/access/check", { json: { rooms } });
    return (await r.json()) as { rooms: AccessRoomAnswer[] };
  }

  async getState(documentId: string): Promise<StoredState> {
    const r = await this.call("GET", `/documents/${documentId}/state`);
    return {
      state: new Uint8Array(await r.arrayBuffer()),
      counter: Number(r.headers.get("X-Update-Counter")),
      collabState: r.headers.get("X-Collab-State") as CollabState,
      roomId: r.headers.get("X-Room-Id") ?? "",
    };
  }
  async putState(documentId: string, token: number, counter: number, state: Uint8Array, insertIfAbsent = false): Promise<void> {
    const headers: Record<string, string> = { "X-Collab-Lease": String(token), "X-Update-Counter": String(counter) };
    if (insertIfAbsent) headers["X-Insert-If-Absent"] = "true";
    await this.call("PUT", `/documents/${documentId}/state`, { body: state, headers });
  }

  async getSeed(documentId: string): Promise<Seed> {
    const r = await this.call("GET", `/documents/${documentId}/seed`);
    return (await r.json()) as Seed;
  }
  async mark(documentId: string, token: number, state: "ready" | "needs_repair", reason: string): Promise<void> {
    await this.call("POST", `/documents/${documentId}/mark`, {
      json: { state, reason },
      headers: { "X-Collab-Lease": String(token) },
    });
  }
  async snapshot(documentId: string, token: number, body: SnapshotIn): Promise<{ stored: boolean; snapshot_id: string | null }> {
    const r = await this.call("POST", `/documents/${documentId}/snapshot`, {
      json: body,
      headers: { "X-Collab-Lease": String(token) },
    });
    return (await r.json()) as { stored: boolean; snapshot_id: string | null };
  }
  async checkAssets(roomId: string, assetIds: string[]): Promise<string[]> {
    const r = await this.call("POST", "/assets/check", { json: { room_id: roomId, asset_ids: assetIds } });
    return ((await r.json()) as { missing: string[] }).missing;
  }
}
