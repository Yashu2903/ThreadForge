/**
 * Recovery copies of text this browser typed but the server never acknowledged (docs/ai.md 19.6).
 *
 * LIFECYCLE
 *   * created when (a) a merge deleted unacknowledged text (the AI replaced the block it was typed in), (b) a
 *     document, room or page was left while text was still unacknowledged, (c) access was lost or lowered;
 *   * kept in this browser only (localStorage), never sent anywhere, so it survives a reload;
 *   * shown to the person who typed it, in the room it came from; inspectable and copyable;
 *   * removed ONLY by a deliberate act: inserting it into the document, or discarding it after a confirmation;
 *     or when it is older than RETENTION_DAYS;
 *   * it is never merged back automatically: an old edit is not assumed to be safe for a document that moved on.
 *
 * BOUNDS (so a bad session cannot fill the disk): MAX_RECORDS per person (oldest dropped first), MAX_BLOCKS per
 * record, MAX_CHARS per text field (longer text is cut and the record says so), RETENTION_DAYS.
 * If the browser refuses to store (private mode, quota) the records stay in memory for this page and `persisted`
 * is false so the screen can say "copy it now".
 *
 * NOT COVERED: text the browser never recorded (a crash, a killed tab, another tab, another device).
 */
export type RecoveryReason = "replaced_while_pending" | "left_with_unsent_changes" | "access_lost" | "became_read_only";

export interface RecoveredBlock {
  blockId: string | null;
  /** only the characters typed since the server last acknowledged this browser */
  inserted: string;
  /** the person's own version of the paragraph those characters were typed into */
  yourVersion: string;
  truncated?: boolean;
}

export interface RecoveryRecord {
  /** `<documentId>|<client id>:<first clock>-<last clock>`: capturing the same characters twice yields one record */
  id: string;
  userId: string;
  roomId: string;
  documentId: string;
  documentTitle: string;
  /** ISO time of the capture */
  capturedAt: string;
  reason: RecoveryReason;
  /** where the shared document stood for this browser: its acknowledged Yjs clock, and the AI proposals the document had applied */
  base: { acknowledgedClock: number; appliedProposals: string[] };
  blocks: RecoveredBlock[];
}

/** A capture from a CollabSession, with who and where it belongs to, as a record ready to store. */
export function recordFrom(
  capture: import("./session").RecoveryCapture,
  context: { userId: string; roomId: string; documentId: string; documentTitle: string },
  now: Date = new Date(),
): RecoveryRecord {
  return {
    id: `${context.documentId}|${capture.captureId}`,
    ...context,
    capturedAt: now.toISOString(),
    reason: capture.reason,
    base: { acknowledgedClock: capture.acknowledgedClock, appliedProposals: capture.appliedProposals },
    blocks: capture.pieces.map((p) => ({ blockId: p.blockId, inserted: p.inserted, yourVersion: p.yourVersion })),
  };
}

export const MAX_RECORDS = 20;
export const MAX_BLOCKS = 50;
export const MAX_CHARS = 20_000;
export const RETENTION_DAYS = 14;

const KEY = (userId: string) => `threadforge.recovery.v1:${userId}`;

function clip(text: string): { text: string; truncated: boolean } {
  return text.length > MAX_CHARS ? { text: text.slice(0, MAX_CHARS), truncated: true } : { text, truncated: false };
}

/** The record as it will be stored: bounded. */
export function bounded(record: RecoveryRecord): RecoveryRecord {
  return {
    ...record,
    documentTitle: record.documentTitle.slice(0, 200),
    base: { ...record.base, appliedProposals: record.base.appliedProposals.slice(-50) },
    blocks: record.blocks.slice(0, MAX_BLOCKS).map((b) => {
      const inserted = clip(b.inserted);
      const mine = clip(b.yourVersion);
      return { blockId: b.blockId, inserted: inserted.text, yourVersion: mine.text, ...(inserted.truncated || mine.truncated || b.truncated ? { truncated: true } : {}) };
    }),
  };
}

function defaultStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null; // access to storage can throw (blocked site data)
  }
}

export class RecoveryStore {
  private memory = new Map<string, RecoveryRecord[]>();
  private listeners = new Set<() => void>();
  private version = 0;
  /** false once a write was refused: the records exist for this page only */
  persisted = true;

  constructor(
    private readonly storage: Storage | null = defaultStorage(),
    private readonly now: () => number = () => Date.now(),
  ) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getVersion = (): number => this.version;

  private read(userId: string): RecoveryRecord[] {
    if (!this.memory.has(userId)) {
      let loaded: RecoveryRecord[] = [];
      try {
        const raw = this.storage?.getItem(KEY(userId));
        const parsed: unknown = raw ? JSON.parse(raw) : [];
        if (Array.isArray(parsed)) loaded = parsed.filter((r): r is RecoveryRecord => !!r && typeof r === "object" && typeof (r as RecoveryRecord).id === "string" && Array.isArray((r as RecoveryRecord).blocks));
      } catch {
        loaded = []; // unreadable or hostile contents are ignored, never trusted
      }
      this.memory.set(userId, loaded);
    }
    const cutoff = this.now() - RETENTION_DAYS * 86_400_000;
    const fresh = this.memory.get(userId)!.filter((r) => Date.parse(r.capturedAt) >= cutoff);
    this.memory.set(userId, fresh);
    return fresh;
  }

  private write(userId: string, records: RecoveryRecord[]): void {
    this.memory.set(userId, records);
    let kept = records;
    for (;;) {
      try {
        this.storage?.setItem(KEY(userId), JSON.stringify(kept));
        this.persisted = this.storage !== null && kept.length === records.length;
        break;
      } catch {
        this.persisted = false;
        if (kept.length <= 1) break; // nothing smaller to try: the records stay in memory only
        kept = kept.slice(1); // drop the oldest and retry
      }
    }
    this.version += 1;
    for (const l of this.listeners) l();
  }

  /** Records of one person, newest first; optionally only one room. */
  list(userId: string, roomId?: string): RecoveryRecord[] {
    return this.read(userId)
      .filter((r) => !roomId || r.roomId === roomId)
      .sort((a, b) => Date.parse(b.capturedAt) - Date.parse(a.capturedAt));
  }

  /** Adds a record. A record with the same id is ignored (no duplicates). Returns whether it was new. */
  add(record: RecoveryRecord): boolean {
    const records = this.read(record.userId);
    if (records.some((r) => r.id === record.id)) return false;
    const next = [...records, bounded(record)];
    this.write(record.userId, next.slice(Math.max(0, next.length - MAX_RECORDS)));
    return true;
  }

  /** Removes one record: only ever called after a deliberate insert or a confirmed discard. */
  remove(userId: string, id: string): void {
    const records = this.read(userId);
    if (records.some((r) => r.id === id)) this.write(userId, records.filter((r) => r.id !== id));
  }
}

export const recoveryStore = new RecoveryStore();
