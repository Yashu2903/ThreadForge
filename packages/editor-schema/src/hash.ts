/**
 * Hashes of validated documents.
 *  - contentHash(doc): SHA-256 of the canonical serialization of the whole document;
 *  - blockHash(block): SHA-256 of ONE block's canonical serialization with every `id` attribute
 *    removed (recursively), so it describes the block's CONTENT. A later AI edit says "I based this on
 *    block X whose hash was H"; if the block changed since, the hashes differ and the edit is refused
 *    instead of merged blindly.
 * SHA-256 comes from the Web Crypto API (Node 19+ and every browser we support).
 */
import { type PMJson, canonicalStringify, canonicalize } from "./canonical.js";

async function sha256Hex(text: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function stripIds(node: PMJson): PMJson {
  const out: PMJson = { ...node };
  if (out.attrs) {
    const { id: _id, ...rest } = out.attrs;
    if (Object.keys(rest).length > 0) out.attrs = rest;
    else delete out.attrs;
  }
  if (out.content) out.content = out.content.map(stripIds);
  return out;
}

export function contentHash(doc: PMJson): Promise<string> {
  return sha256Hex(canonicalStringify(canonicalize(doc)));
}

export function blockHash(block: PMJson): Promise<string> {
  return sha256Hex(canonicalStringify(stripIds(canonicalize(block))));
}

/** [{ id, hash }] for every top-level block of a document. */
export async function blockHashes(doc: PMJson): Promise<{ id: string | null; type: string; hash: string }[]> {
  const out: { id: string | null; type: string; hash: string }[] = [];
  for (const block of doc.content ?? []) {
    out.push({ id: (block.attrs?.id as string | undefined) ?? null, type: block.type ?? "?", hash: await blockHash(block) });
  }
  return out;
}
