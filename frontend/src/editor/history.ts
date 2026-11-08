/**
 * Application-level undo/redo over whole-document snapshots.
 *
 * A snapshot is just a pointer to an immutable document, so keeping many is cheap: documents share
 * every block an edit did not touch. The browser's own undo stack is never used (the editor cancels
 * history input types), so undo can only ever produce a document that was valid when it was recorded.
 *
 * GROUPING. Ordinary typing in one block, where each keystroke continues from the caret the last one
 * left and follows it within one second, is ONE entry. Everything else (formatting, Enter, deletes,
 * paste, structural commands) is its own entry.
 *
 * BOUNDS. At most `maxEntries` entries and about `maxBytes` of NEWLY allocated blocks (estimated per
 * top-level block that differs from the previous snapshot). The oldest entries are dropped first.
 *
 * Loading a document is not an undo step (it is the base). Saving does not clear history. A fresh
 * history is made when a different document is loaded or when a newer server version of a clean
 * document is adopted (the old snapshots describe a document that no longer exists).
 * This is NOT durable or collaborative history: it lives in this tab and dies with it.
 */
import { type Doc, type Node } from "./doc";
import { type Sel } from "./commands";

export interface Entry {
  doc: Doc;
  selBefore: Sel | null; // the selection to restore when this entry is undone (null for the base)
  selAfter: Sel;
  kind: string;
  key: string | null;
  time: number;
  cost: number;
}

export interface HistoryOptions {
  maxEntries?: number;
  maxBytes?: number;
  groupMs?: number;
  now?: () => number;
}

const sizeCache = new WeakMap<object, number>();

/** A rough byte estimate of a node: 2 bytes per UTF-16 unit of text plus a fixed cost per node. */
export function approxBytes(node: Node): number {
  const hit = sizeCache.get(node);
  if (hit !== undefined) return hit;
  let n = 96;
  if (node.type === "paragraph" || node.type === "heading") {
    for (const c of node.children ?? []) n += c.type === "text" ? 48 + 2 * c.text.length : 96;
  } else for (const k of node.children as Node[]) n += approxBytes(k);
  sizeCache.set(node, n);
  return n;
}

const sameSel = (a: Sel, b: Sel): boolean =>
  a.anchor.id === b.anchor.id && a.anchor.offset === b.anchor.offset && a.focus.id === b.focus.id && a.focus.offset === b.focus.offset;

export class History {
  private entries: Entry[] = [];
  private index = 0;
  private sealed = false; // no merging into the current top entry
  private readonly maxEntries: number;
  private readonly maxBytes: number;
  private readonly groupMs: number;
  private readonly now: () => number;

  constructor(base: Doc, sel: Sel, options: HistoryOptions = {}) {
    this.maxEntries = options.maxEntries ?? 100;
    this.maxBytes = options.maxBytes ?? 30 * 1024 * 1024;
    this.groupMs = options.groupMs ?? 1000;
    this.now = options.now ?? (() => Date.now());
    this.reset(base, sel);
  }

  reset(base: Doc, sel: Sel): void {
    this.entries = [{ doc: base, selBefore: null, selAfter: sel, kind: "base", key: null, time: this.now(), cost: 0 }];
    this.index = 0;
    this.sealed = true;
  }

  get size(): number {
    return this.entries.length - 1; // undoable steps
  }
  get totalCost(): number {
    return this.entries.reduce((n, e) => n + e.cost, 0);
  }
  get canUndo(): boolean {
    return this.index > 0;
  }
  get canRedo(): boolean {
    return this.index < this.entries.length - 1;
  }
  get current(): Entry {
    return this.entries[this.index]!;
  }

  /** The caret moved on its own (a click, an arrow key): the next keystroke starts a new group. */
  seal(): void {
    this.sealed = true;
  }

  record(doc: Doc, selBefore: Sel, selAfter: Sel, kind: string, key: string | null = null): void {
    const top = this.entries[this.index]!;
    if (doc === top.doc) return;
    const t = this.now();
    const mergeable =
      kind === "typing" &&
      top.kind === "typing" &&
      key !== null &&
      top.key === key &&
      !this.sealed &&
      this.index === this.entries.length - 1 &&
      t - top.time < this.groupMs &&
      sameSel(top.selAfter, selBefore);
    if (mergeable) {
      const prev = this.entries[this.index - 1]!;
      this.entries[this.index] = { ...top, doc, selAfter, time: t, cost: costOf(doc, prev.doc) };
    } else {
      this.entries.length = this.index + 1; // a new edit discards the redo branch
      this.entries.push({ doc, selBefore, selAfter, kind, key, time: t, cost: costOf(doc, top.doc) });
      this.index++;
    }
    this.sealed = false;
    this.trim();
  }

  undo(): { doc: Doc; sel: Sel } | null {
    if (!this.canUndo) return null;
    const undone = this.entries[this.index]!;
    this.index--;
    this.sealed = true;
    return { doc: this.entries[this.index]!.doc, sel: undone.selBefore! };
  }

  redo(): { doc: Doc; sel: Sel } | null {
    if (!this.canRedo) return null;
    this.index++;
    this.sealed = true;
    const e = this.entries[this.index]!;
    return { doc: e.doc, sel: e.selAfter };
  }

  private trim(): void {
    let bytes = this.totalCost;
    // the byte bound never drops the most recent step: one huge edit must still be undoable once
    while (this.entries.length > 1 && (this.entries.length - 1 > this.maxEntries || (bytes > this.maxBytes && this.entries.length > 2))) {
      bytes -= this.entries[1]!.cost;
      this.entries.shift(); // the next entry becomes the base
      this.entries[0] = { ...this.entries[0]!, selBefore: null, cost: 0 };
      this.index = Math.max(0, this.index - 1);
    }
  }
}

function costOf(doc: Doc, previous: Doc): number {
  const known = new Set<object>(previous.blocks);
  let n = 0;
  for (const b of doc.blocks) if (!known.has(b)) n += approxBytes(b);
  return n;
}
