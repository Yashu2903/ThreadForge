/**
 * Wire types and constants shared with the backend (schema_version 1).
 * See backend/app/realtime/protocol.py for the authoritative definition.
 */

export const SCHEMA_VERSION = 1;

// Close codes (one table for Parts 4 and 5; reactions are in room-client.ts `reactionFor`).
export const Close = {
  GOING_AWAY: 1001,
  UNSUPPORTED_DATA: 1003,
  MESSAGE_TOO_BIG: 1009,
  TRY_AGAIN_LATER: 1013,
  PROTOCOL_VIOLATION: 4400,
  SESSION_INVALID: 4401,
  ACCESS_REVOKED: 4403,
  TIMEOUT: 4408,
  TOO_MANY_CONNECTIONS: 4409,
  RESYNC_REQUIRED: 4410,
  SLOW_CONSUMER: 4429,
} as const;

export interface Member {
  user_id: string;
  display_name: string;
  role: "owner" | "editor" | "viewer";
  joined_at: string;
}

export interface Message {
  id: string;
  room_id: string;
  author_id: string;
  author_display_name: string;
  client_message_id: string;
  body: string | null; // null for tombstones
  created_at: string;
  edited_at: string | null;
  deleted_at: string | null;
  deleted_by: string | null;
  version: number;
  is_deleted: boolean;
}

export interface RoomInfo {
  id: string;
  name: string;
  owner_id: string;
  created_at: string;
  updated_at: string;
  /** Part 6: the room's one shared active document. Moves forward only (active_document_version). */
  active_document_id: string | null;
  active_document_version: number;
}

/** What one batch of events says about documents, coalesced by document id (Part 6). */
export interface DocumentChange {
  created: boolean;
  renamed: boolean;
  /** The highest content_version announced in the batch (null if no content event). */
  contentVersion: number | null;
}
export interface DocumentSignals {
  /** The document list (metadata) may be stale. */
  listChanged: boolean;
  documents: Map<string, DocumentChange>;
  /** The newest selection announced in the batch, if any (already applied to state.room). */
  active: { documentId: string; selectionVersion: number } | null;
  /** The room state was replaced by a fresh bootstrap: refetch everything you hold. */
  reset: boolean;
}

/** GET /rooms/{id}/sync */
export interface Bootstrap {
  room: RoomInfo;
  role: string;
  membership_epoch: number;
  members: Member[];
  messages: Message[];
  next_before: string | null;
  through_seq: number;
}

export interface RoomEvent {
  type: "room_event";
  schema_version: number;
  event_id: string;
  room_id: string;
  room_seq: number;
  event_type: string;
  resource_type: string;
  resource_id: string | null;
  resource_version: number | null;
  payload: Record<string, unknown>;
  actor_id: string;
  created_at: string;
}

export type Availability = "available" | "busy" | "dnd";

export type ServerFrame =
  | { type: "sync_started"; schema_version: number; room_id: string; after_seq: number; through_seq: number }
  | RoomEvent
  | { type: "sync_complete"; through_seq: number }
  | { type: "heartbeat_ack"; server_time: string }
  | { type: "presence_snapshot"; room_id: string; online: { user_id: string; availability: Availability }[]; degraded: boolean }
  | { type: "typing_state"; room_id: string; typing: string[]; degraded: boolean }
  | { type: "resync_required"; reason: "cursor_ahead" | "too_far_behind" | "event_gap" };

export type ClientFrame =
  | { type: "resume"; schema_version: 1; after_seq: number }
  | { type: "ack"; through_seq: number }
  | { type: "heartbeat" }
  | { type: "typing"; state: "start" | "stop" }
  | { type: "set_availability"; value: Availability };

export type Status = "bootstrapping" | "syncing" | "live" | "reconnecting" | "resyncing" | "terminal";

export type TerminalReason =
  | "access_lost" // 4403, member_removed for me, or a fetch said 401/403/404: room data is cleared
  | "sign_in_required" // 4401
  | "protocol_error" // 1003, 1009, 4400
  | "too_many_connections" // 4409 three times
  | "cannot_synchronize" // resync-loop breaker
  | "stopped"; // the application called stop()
