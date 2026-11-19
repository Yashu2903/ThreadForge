/**
 * Finding the text this browser typed that the server has not acknowledged, and noticing when a merge destroys it.
 *
 * WHY THIS EXISTS (docs/ai.md 19.6): when the server replaces a block's text it deletes the block's old Y.XmlText
 * node. Characters this browser typed INSIDE that node while offline are integrated under a deleted parent when
 * they finally merge, so Yjs deletes them and garbage collects them. This class sees that moment (a REMOTE
 * transaction that deletes this browser's still-unacknowledged items) and returns the lost text so it can be kept.
 *
 * WHAT "UNACKNOWLEDGED" MEANS HERE. Every item this browser creates has an id `(clientID, clock)`. `acknowledged` is
 * the clock this browser had reached the last time the provider reported "the server has acknowledged everything"
 * (`unsyncedChanges === 0`). The provider resets its counter to 1 on every (re)connect and the server answers
 * only after it has APPLIED this browser's changes (node_modules/@hocuspocus/server MessageReceiver: SyncStatus
 * follows readSyncStep2 / readUpdate), so reaching 0 does mean the server holds them. Items with a clock at or
 * beyond `acknowledged` are the ones the server may never have seen.
 *
 * WHAT IT DOES NOT DO. It only knows what is in this browser's Y.Doc. Edits the browser never recorded (a crash, a
 * killed tab, text typed in another tab) are outside it. It keeps typed TEXT, not formatting or pending deletions.
 *
 * `afterTransaction` runs BEFORE Yjs garbage-collects deleted content (yjs Transaction.js: the event is emitted,
 * then `tryGcDeleteSet`), which is the only moment the lost characters can still be read.
 */
import * as Y from "yjs";

export interface PendingPiece {
  /** the `id` attribute of the nearest block holding the text, or null when it cannot be told */
  blockId: string | null;
  /** only the characters typed since the server last acknowledged this browser */
  inserted: string;
  /** this person's own version of the text node those characters were typed into, before the merge */
  yourVersion: string;
  /** the Yjs clocks of the characters, used to recognise the same text if it is captured twice */
  clocks: number[];
}

type AnyType = Y.AbstractType<any>; // eslint-disable-line @typescript-eslint/no-explicit-any

/** The nearest ancestor element (or the element itself) that has a string `id` attribute. Reads the raw attribute so it
 *  still works when the element was just deleted and `getAttribute` would answer undefined. */
function blockIdOf(start: AnyType | null): string | null {
  let t: AnyType | null = start;
  while (t) {
    if (t instanceof Y.XmlElement) {
      const attribute = (t as unknown as { _map: Map<string, Y.Item> })._map.get("id");
      const value = attribute?.content.getContent()[0];
      if (typeof value === "string" && value) return value;
    }
    const parent: unknown = (t as unknown as { _item: Y.Item | null })._item?.parent;
    t = parent instanceof Y.AbstractType ? (parent as AnyType) : null;
  }
  return null;
}

/** The text of a node as this person saw it: live items, plus items deleted by THIS transaction (so the text the merge
 *  just destroyed is still included), but never items that were already deleted before it. */
function viewText(type: AnyType, tr?: Y.Transaction): string {
  let out = "";
  for (let item = (type as unknown as { _start: Y.Item | null })._start; item; item = item.right) {
    if (item.deleted && !(tr && Y.isDeleted(tr.deleteSet, item.id))) continue;
    const content = item.content;
    if (content instanceof Y.ContentString) out += content.str;
    else if (content instanceof Y.ContentType) out += viewText(content.type as AnyType, tr);
  }
  return out;
}

function isInside(type: AnyType | null, covered: Set<AnyType>): boolean {
  let t: AnyType | null = type;
  while (t) {
    if (covered.has(t)) return true;
    const parent: unknown = (t as unknown as { _item: Y.Item | null })._item?.parent;
    t = parent instanceof Y.AbstractType ? (parent as AnyType) : null;
  }
  return false;
}

export class PendingTracker {
  private acked = 0;
  /** clocks already returned, so the same characters are never captured twice (repeat reconnects, access loss then destroy) */
  private readonly returned = new Set<number>();

  constructor(private readonly doc: Y.Doc) {}

  /** The server has acknowledged everything this browser has created so far. */
  acknowledge(): void {
    this.acked = Y.getState(this.doc.store, this.doc.clientID);
  }

  get acknowledgedClock(): number {
    return this.acked;
  }

  /** Is there anything this browser created that the server has not acknowledged? */
  get hasUnacknowledged(): boolean {
    return Y.getState(this.doc.store, this.doc.clientID) > this.acked;
  }

  /** Unacknowledged items that the (remote) transaction `tr` just deleted: the text a merge destroyed. */
  lostIn(tr: Y.Transaction): PendingPiece[] {
    if (!this.hasUnacknowledged) return [];
    return this.collect((item) => Y.isDeleted(tr.deleteSet, item.id), tr);
  }

  /** Unacknowledged items that are still in the document: what would be lost if this browser went away now. */
  alive(): PendingPiece[] {
    if (!this.hasUnacknowledged) return [];
    return this.collect((item) => !item.deleted);
  }

  private collect(include: (item: Y.Item) => boolean, tr?: Y.Transaction): PendingPiece[] {
    const structs = (this.doc.store.clients.get(this.doc.clientID) ?? []) as Array<Y.Item | Y.GC>;
    const found: { item: Y.Item; from: number }[] = [];
    for (let i = structs.length - 1; i >= 0; i -= 1) {
      const s = structs[i]!;
      if (s.id.clock + s.length <= this.acked) break; // the array is sorted by clock: everything before is acknowledged
      if (!(s instanceof Y.Item) || !include(s)) continue;
      found.push({ item: s, from: Math.max(0, this.acked - s.id.clock) });
    }
    found.reverse();

    // 1. whole nodes created while unacknowledged (e.g. a new paragraph): their text comes with the node
    const covered = new Set<AnyType>();
    const groups = new Map<AnyType, { blockId: string | null; inserted: string; yourVersion: string; clocks: number[] }>();
    const add = (key: AnyType, blockId: string | null, inserted: string, yourVersion: string, clock: number, length: number) => {
      const clocks = Array.from({ length }, (_, k) => clock + k);
      if (clocks.every((c) => this.returned.has(c))) return;
      const group = groups.get(key) ?? { blockId, inserted: "", yourVersion, clocks: [] };
      group.inserted += inserted;
      group.clocks.push(...clocks);
      groups.set(key, group);
    };
    for (const { item } of found) {
      if (item.content instanceof Y.ContentType) {
        const outer = item.parent instanceof Y.AbstractType ? (item.parent as AnyType) : null;
        if (isInside(outer, covered)) continue; // part of a bigger new node that is already reported (outer nodes come first)
        const node = item.content.type as AnyType;
        covered.add(node);
        const text = viewText(node, tr);
        add(node, blockIdOf(node), text, text, item.id.clock, 1);
      }
    }
    // 2. characters typed into an existing node
    for (const { item, from } of found) {
      if (!(item.content instanceof Y.ContentString)) continue;
      const parent = item.parent instanceof Y.AbstractType ? (item.parent as AnyType) : null;
      if (!parent || isInside(parent, covered)) continue;
      add(parent, blockIdOf(parent), item.content.str.slice(from), viewText(parent, tr), item.id.clock + from, item.length - from);
    }

    const pieces: PendingPiece[] = [];
    for (const group of groups.values()) {
      if (group.inserted.trim() === "") continue; // whitespace alone is not worth recovering
      for (const c of group.clocks) this.returned.add(c);
      pieces.push(group);
    }
    return pieces;
  }
}
