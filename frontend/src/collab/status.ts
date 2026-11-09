/**
 * What the sync indicator says, derived from the provider's raw state. A pure function, so every
 * wording rule is testable without a socket.
 *
 * Wording (precise on purpose):
 *   "Synced"      the SERVER has received every change made here. It is then written to PostgreSQL
 *                 within a few seconds; a hard server crash in that window can lose those seconds.
 *   "Syncing"     connected, but the first sync is not finished or changes are still on their way.
 *   "Offline (n unsynced changes)"  no connection; edits stay in this tab (there is NO offline
 *                 storage: closing the tab loses them) and are sent when the connection returns.
 *   "Read-only"   you may read this document live but not change it.
 *   "Access lost" the server ended your access (session ended, or you were removed from the room).
 */

/** Close codes the collaboration service uses (docs/collaboration.md 4.7). */
export const CLOSE = {
  /** the session is no longer valid */
  unauthorized: 4401,
  /** not a member of the room (any more) */
  forbidden: 4403,
  /** a viewer-downgrade that could not be applied live, or a timeout: reconnect */
  timeout: 4408,
  /** going away (server restart) */
  goingAway: 1001,
  /** try again later (server or its API is unavailable) */
  tryAgainLater: 1013,
  /** a message was larger than allowed */
  messageTooBig: 1009,
} as const;

/** Codes after which reconnecting is pointless: the person no longer has access. */
export const TERMINAL_CODES: readonly number[] = [CLOSE.unauthorized, CLOSE.forbidden];

/** Denial reasons from the collaboration service after which retrying cannot help. */
export const TERMINAL_DENIALS: readonly string[] = ["session_invalid", "forbidden", "origin", "bad_document", "needs_repair"];

export type SyncKind = "connecting" | "synced" | "syncing" | "offline" | "read-only" | "access-lost";

export interface RawState {
  connection: "connecting" | "connected" | "disconnected";
  /** the provider finished its first sync with the server */
  synced: boolean;
  /** local changes the server has not acknowledged yet */
  unsynced: number;
  /** the scope the server granted, once authenticated */
  scope: "read-write" | "readonly" | undefined;
  /** from the document API: may this person edit? */
  canEdit: boolean;
  /** the server refused us (a permission-denied message) or closed with a terminal code */
  accessLost: boolean;
}

export interface SyncStatus {
  kind: SyncKind;
  label: string;
  /** local edits that may be lost if the tab closes right now */
  atRisk: number;
  /** the editor accepts typing */
  editable: boolean;
}

export function deriveStatus(raw: RawState): SyncStatus {
  const readOnly = !raw.canEdit || raw.scope === "readonly";
  if (raw.accessLost) {
    return { kind: "access-lost", label: "Access lost", atRisk: raw.unsynced, editable: false };
  }
  if (raw.connection !== "connected") {
    if (raw.connection === "connecting" && !raw.synced && raw.unsynced === 0) {
      return { kind: "connecting", label: "Connecting…", atRisk: 0, editable: !readOnly };
    }
    const label = raw.unsynced > 0 ? `Offline (${raw.unsynced} unsynced ${raw.unsynced === 1 ? "change" : "changes"})` : "Offline";
    return { kind: "offline", label, atRisk: raw.unsynced, editable: !readOnly };
  }
  if (!raw.synced || raw.unsynced > 0) {
    return { kind: "syncing", label: "Syncing…", atRisk: raw.unsynced, editable: !readOnly };
  }
  if (readOnly) return { kind: "read-only", label: "Read-only", atRisk: 0, editable: false };
  return { kind: "synced", label: "Synced", atRisk: 0, editable: true };
}
