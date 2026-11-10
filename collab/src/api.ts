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
/** What a store carries when it also records that a proposal was applied. */
export interface ApplyReceipt {
  proposalId: string;
  userId: string;
  receiptHash: string;
}

export interface BeginApply {
  /** true: this proposal was applied before; nothing to do but report it */
  already_applied: boolean;
  proposal_id: string;
  document_id: string;
  room_id: string;
  operations: unknown;
  base_hashes: Record<string, string>;
  scope: { whole_ids?: string[]; ranges?: { block_id: string; start: number; end: number }[]; anchors?: string[] };
  captured_counter: number | null;
  resulting_counter?: number | null;
}

export interface StuckProposal {
  proposal_id: string;
  document_id: string;
  applying_by: string;
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
  async putState(
    documentId: string,
    token: number,
    counter: number,
    state: Uint8Array,
    insertIfAbsent = false,
    receipt?: ApplyReceipt,
  ): Promise<void> {
    const headers: Record<string, string> = { "X-Collab-Lease": String(token), "X-Update-Counter": String(counter) };
    if (insertIfAbsent) headers["X-Insert-If-Absent"] = "true";
    if (receipt) {
      // the application receipt is written by the SAME database transaction that stores these bytes
      headers["X-Proposal-Id"] = receipt.proposalId;
      headers["X-Applied-By"] = receipt.userId;
      headers["X-Receipt-Hash"] = receipt.receiptHash;
    }
    await this.call("PUT", `/documents/${documentId}/state`, { body: state, headers });
  }

  // ---- AI proposals (phase 2): the authority checks and state changes live in PostgreSQL ----------------

  /** Recheck the user's write access and the proposal, and move pending -> applying (compare-and-set). */
  async beginApply(proposalId: string, token: number, body: { user_id: string; document_id: string }): Promise<BeginApply> {
    const r = await this.call("POST", `/proposals/${proposalId}/begin-apply`, { json: body, headers: { "X-Collab-Lease": String(token) } });
    return (await r.json()) as BeginApply;
  }
  async finishApply(proposalId: string, token: number, body: { user_id: string; resulting_counter: number; receipt_hash: string }): Promise<{ state: string }> {
    const r = await this.call("POST", `/proposals/${proposalId}/finish-apply`, { json: body, headers: { "X-Collab-Lease": String(token) } });
    return (await r.json()) as { state: string };
  }
  /** applying -> pending (nothing was changed): the document was busy, or the apply failed before any change */
  async releaseApply(proposalId: string, token: number, reason: string): Promise<void> {
    await this.call("POST", `/proposals/${proposalId}/release-apply`, { json: { reason }, headers: { "X-Collab-Lease": String(token) } });
  }
  /** applying -> conflicted: the document no longer matches what the proposal was based on */
  async markConflicted(proposalId: string, token: number, body: { user_id: string; code: string }): Promise<void> {
    await this.call("POST", `/proposals/${proposalId}/conflicted`, { json: body, headers: { "X-Collab-Lease": String(token) } });
  }
  /** proposals that have been `applying` for longer than `olderThanSeconds` (the recovery sweeper's work list) */
  async stuckProposals(olderThanSeconds: number): Promise<StuckProposal[]> {
    const r = await this.call("GET", `/proposals/stuck?older_than_seconds=${olderThanSeconds}`);
    return ((await r.json()) as { proposals: StuckProposal[] }).proposals;
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
