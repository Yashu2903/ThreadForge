/**
 * Cheap document counters, to refuse an edit that would break a limit BEFORE it is applied (the
 * server enforces the same limits on save; this just keeps the user from typing into a wall).
 * Per-node results are cached by object identity, and structural sharing means an edit only
 * recomputes the nodes on the path it changed.
 */
import type { Limits } from "threadforge-client/document-schema";
import { type Doc, type Node, isTextBlock } from "./doc";

export interface Stats {
  textCp: number;
  nodes: number;
  blocks: number;
  inline: number;
  images: number;
}

const cache = new WeakMap<object, Stats>();
const ZERO: Stats = { textCp: 0, nodes: 0, blocks: 0, inline: 0, images: 0 };

function codePoints(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) i++;
    n++;
  }
  return n;
}

function statsOf(node: Node): Stats {
  const hit = cache.get(node);
  if (hit) return hit;
  const s: Stats = { textCp: 0, nodes: 1, blocks: 0, inline: 0, images: 0 };
  if (node.type !== "table_row" && node.type !== "table_cell") s.blocks = 1;
  if (isTextBlock(node)) {
    for (const c of node.children ?? []) {
      s.nodes++;
      s.inline++;
      if (c.type === "text") s.textCp += codePoints(c.text);
      else if (c.type === "image") s.images++;
    }
  } else {
    for (const child of node.children as Node[]) {
      const k = statsOf(child);
      s.textCp += k.textCp;
      s.nodes += k.nodes;
      s.blocks += k.blocks;
      s.inline += k.inline;
      s.images += k.images;
    }
  }
  cache.set(node, s);
  return s;
}

export function docStats(doc: Doc): Stats {
  const total = { ...ZERO };
  for (const b of doc.blocks) {
    const k = statsOf(b);
    total.textCp += k.textCp;
    total.nodes += k.nodes;
    total.blocks += k.blocks;
    total.inline += k.inline;
    total.images += k.images;
  }
  return total;
}

/** A message when `after` breaks a limit that `before` did not already break; otherwise null. */
export function limitProblem(before: Doc, after: Doc, limits: Limits): string | null {
  const a = docStats(after);
  const b = docStats(before);
  const checks: [number, number, number, string][] = [
    [a.textCp, b.textCp, limits.maxTextCodepoints, `The document is limited to ${limits.maxTextCodepoints.toLocaleString()} characters.`],
    [a.nodes, b.nodes, limits.maxTotalNodes, "The document has reached its size limit."],
    [a.blocks, b.blocks, limits.maxBlocks, `The document is limited to ${limits.maxBlocks.toLocaleString()} paragraphs and list items.`],
    [a.inline, b.inline, limits.maxInlineNodes, "The document has too many line breaks, images and formatting changes."],
    [a.images, b.images, limits.maxImages, `A document can hold at most ${limits.maxImages} images.`],
  ];
  for (const [now, was, max, message] of checks) if (now > max && now > was) return message;
  return null;
}
