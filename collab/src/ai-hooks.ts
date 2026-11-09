/**
 * AI-ready hooks. NO AI lives here: no model, no prompt, no provider. These are the two operations a
 * later AI feature needs from the collaboration service, built and tested now so the rules are fixed
 * before any caller exists:
 *
 *   READ   `POST /internal/ai/read`   {document_id, requester_id}
 *          The document as it is NOW: pending edits are stored first, so the answer carries the exact
 *          `yjs_update_counter` of the stored state it describes, a `content_hash` of the validated
 *          content, and for every block with an id its own `hash` (content only, ids ignored), type,
 *          parent and plain text.
 *
 *   APPLY  `POST /internal/ai/apply`  {document_id, requester_id, block_id, base_hash, block}
 *          Replace ONE block, found by its stable id, with a new block, if and only if
 *            1. the requester may write to the document RIGHT NOW (checked on every call through the
 *               same decide_write as the editor, never cached);
 *            2. the block's current hash equals `base_hash` ("I based this on version H"): a block
 *               that changed in the meantime is refused with 409 instead of being merged blindly;
 *            3. the resulting whole document passes the shared schema validation;
 *            4. size and frequency limits hold (per requester and per document, per minute).
 *          The change is a Yjs transaction with its OWN origin
 *          `{source: "local", context: {source: "ai-hook", requester_id}}`, so it is distinguishable from
 *          every browser edit, and it merges with whatever collaborators are typing elsewhere (only the
 *          target block is replaced, never the whole document).
 *
 * Everything is secret-protected (the shared INTERNAL_API_SECRET) and private to the network like the
 * rest of /internal. Logs carry ids, sizes and result codes only: never document text, never the block.
 */
import {
  ID_TYPES,
  type PMJson,
  YJS_FIELD,
  blockHash,
  canonicalStringify,
  canonicalize,
  contentHash,
  extractText,
  getEditorSchema,
  validateDocument,
} from "@threadforge/editor-schema";
import { prosemirrorJSONToYXmlFragment, yDocToProsemirrorJSON } from "@tiptap/y-tiptap";
import { randomUUID } from "node:crypto";
import * as Y from "yjs";
import { ApiError, type InternalApi } from "./api.js";
import type { CollabConfig } from "./config.js";
import { validatedSnapshotOfState } from "./documents.js";
import type { Logger } from "./log.js";
import type { CollabServer } from "./server.js";

export interface Reply {
  status: number;
  body: unknown;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const HASH = /^[0-9a-f]{64}$/;
/** node types that carry an id: the SAME list the editor and the validator use */
const ID_TYPE_SET = new Set<string>(ID_TYPES);

export interface BlockInfo {
  id: string;
  type: string;
  hash: string;
  parent_id: string | null;
  text: string;
}

/** Every node that has an id, in document order, with its content hash. */
export async function collectBlocks(doc: PMJson, cap = 5000): Promise<BlockInfo[]> {
  const out: BlockInfo[] = [];
  const walk = async (nodes: PMJson[] | undefined, parent: string | null): Promise<void> => {
    for (const node of nodes ?? []) {
      const id = node.attrs?.id;
      let next = parent;
      if (typeof id === "string" && out.length < cap) {
        out.push({
          id,
          type: node.type ?? "?",
          hash: await blockHash(node),
          parent_id: parent,
          text: extractText({ type: "doc", content: [node] }),
        });
        next = id;
      }
      await walk(node.content, next);
    }
  };
  await walk(doc.content, null);
  return out;
}

/** The path (array of child indexes) to the node with this id, or null. */
function findPath(nodes: PMJson[] | undefined, id: string, prefix: number[] = []): number[] | null {
  for (let i = 0; i < (nodes?.length ?? 0); i += 1) {
    const node = nodes![i]!;
    if (node.attrs?.id === id) return [...prefix, i];
    const inner = findPath(node.content, id, [...prefix, i]);
    if (inner) return inner;
  }
  return null;
}

function nodeAt(doc: PMJson, path: number[]): PMJson {
  let node: PMJson = doc;
  for (const i of path) node = node.content![i]!;
  return node;
}

function replaceAt(doc: PMJson, path: number[], replacement: PMJson): PMJson {
  const copy: PMJson = structuredClone(doc);
  let parent = copy;
  for (const i of path.slice(0, -1)) parent = parent.content![i]!;
  parent.content![path[path.length - 1]!] = replacement;
  return copy;
}

/** The replacement keeps the target's id; nodes that need an id and have none get a fresh one. */
function prepareBlock(block: PMJson, id: string): PMJson {
  const fill = (node: PMJson, isRoot: boolean): PMJson => {
    const out: PMJson = { ...node };
    if (ID_TYPE_SET.has(node.type ?? "")) {
      out.attrs = { ...(node.attrs ?? {}), id: isRoot ? id : (node.attrs?.id as string | undefined) ?? randomUUID() };
    }
    if (node.content) out.content = node.content.map((child) => fill(child, false));
    return out;
  };
  return fill(block, true);
}

function findYElement(parent: Y.XmlFragment | Y.XmlElement, id: string): { parent: Y.XmlFragment | Y.XmlElement; index: number } | null {
  const children = parent.toArray();
  for (let i = 0; i < children.length; i += 1) {
    const child = children[i];
    if (!(child instanceof Y.XmlElement)) continue;
    if (child.getAttribute("id") === id) return { parent, index: i };
    const inner = findYElement(child, id);
    if (inner) return inner;
  }
  return null;
}

/** A detached Y element for this block, built by the SAME library the editor uses. */
function yBlock(block: PMJson): Y.XmlElement {
  const scratch = new Y.Doc();
  try {
    const fragment = scratch.getXmlFragment("x");
    prosemirrorJSONToYXmlFragment(getEditorSchema(), { type: "doc", content: [block] }, fragment);
    return (fragment.get(0) as Y.XmlElement).clone();
  } finally {
    scratch.destroy();
  }
}

export function replaceInYjs(doc: Y.Doc, id: string, block: PMJson): void {
  const hit = findYElement(doc.getXmlFragment(YJS_FIELD), id);
  if (!hit) throw new Error("block_not_found");
  hit.parent.delete(hit.index, 1);
  hit.parent.insert(hit.index, [yBlock(block)]);
}

class Window {
  private readonly hits = new Map<string, number[]>();
  constructor(private readonly perMinute: number) {}
  /** true if allowed (and counts it) */
  take(key: string, now = Date.now()): boolean {
    const recent = (this.hits.get(key) ?? []).filter((t) => t > now - 60_000);
    if (recent.length >= this.perMinute) {
      this.hits.set(key, recent);
      return false;
    }
    recent.push(now);
    this.hits.set(key, recent);
    return true;
  }
}

export class AiHooks {
  private readonly byRequester: Window;
  private readonly byDocument: Window;

  constructor(
    private readonly server: CollabServer,
    private readonly api: InternalApi,
    private readonly config: CollabConfig,
    private readonly log: Logger,
  ) {
    this.byRequester = new Window(config.aiAppliesPerMinute);
    this.byDocument = new Window(config.aiAppliesPerDocumentPerMinute);
  }

  private async permission(requester: string, documentId: string): Promise<Reply | null> {
    try {
      const decision = await this.api.canWrite(requester, documentId);
      if (decision.allowed) return null;
      return { status: 403, body: { error: "forbidden", reason: decision.reason } };
    } catch (error) {
      this.log.warn("ai hook: permission check failed", { document: documentId, code: error instanceof ApiError ? error.code : "error" });
      return { status: 503, body: { error: "unavailable" } };
    }
  }

  async read(input: { document_id?: unknown; requester_id?: unknown }): Promise<Reply> {
    const { document_id: documentId, requester_id: requester } = input;
    if (typeof documentId !== "string" || !UUID.test(documentId) || typeof requester !== "string" || !UUID.test(requester)) {
      return { status: 422, body: { error: "invalid_request" } };
    }
    const denied = await this.permission(requester, documentId);
    if (denied) return denied;
    try {
      return await this.server.withDocument(documentId, { source: "ai-hook", requester_id: requester }, async ({ document, name }) => {
        const stored = await this.server.storeNow(name, document);
        const outcome = await validatedSnapshotOfState(stored.bytes, this.config.limits);
        if (!outcome.ok) return { status: 409, body: { error: "document_invalid" } };
        const blocks = await collectBlocks(outcome.data.content);
        this.log.info("ai read", { document: documentId, counter: stored.counter, blocks: blocks.length });
        return {
          status: 200,
          body: {
            document_id: documentId,
            yjs_update_counter: stored.counter,
            content_hash: outcome.data.contentHash,
            content: outcome.data.content,
            blocks,
          },
        };
      });
    } catch (error) {
      this.log.error("ai read failed", { document: documentId, error: error instanceof Error ? error.name : "error" });
      return { status: 503, body: { error: "unavailable" } };
    }
  }

  async apply(input: { document_id?: unknown; requester_id?: unknown; block_id?: unknown; base_hash?: unknown; block?: unknown }): Promise<Reply> {
    const { document_id: documentId, requester_id: requester, block_id: blockId, base_hash: baseHash, block } = input;
    if (
      typeof documentId !== "string" || !UUID.test(documentId) ||
      typeof requester !== "string" || !UUID.test(requester) ||
      typeof blockId !== "string" || !UUID.test(blockId) ||
      typeof baseHash !== "string" || !HASH.test(baseHash) ||
      typeof block !== "object" || block === null || Array.isArray(block) || typeof (block as PMJson).type !== "string"
    ) {
      return { status: 422, body: { error: "invalid_request" } };
    }
    const blockText = JSON.stringify(block);
    if (Buffer.byteLength(blockText) > this.config.aiMaxBlockBytes) return { status: 413, body: { error: "block_too_large" } };
    if (!this.byRequester.take(requester) || !this.byDocument.take(documentId)) {
      this.log.warn("ai apply rate limited", { document: documentId });
      return { status: 429, body: { error: "rate_limited" } };
    }
    const denied = await this.permission(requester, documentId);
    if (denied) return denied;

    try {
      return await this.server.withDocument(documentId, { source: "ai-hook", requester_id: requester }, async ({ document, name, direct }) => {
        // 1. where is the block, and has it changed since the caller looked?
        const current = yDocToProsemirrorJSON(document, YJS_FIELD) as PMJson;
        const path = findPath(current.content, blockId);
        if (!path) return { status: 404, body: { error: "block_not_found" } };
        const target = nodeAt({ content: current.content } as PMJson, path);
        const currentHash = await blockHash(target);
        if (currentHash !== baseHash) return { status: 409, body: { error: "block_changed", current_hash: currentHash } };
        const targetSnapshot = canonicalStringify(canonicalize(target));

        // 2. the whole document with the replacement must still be valid
        const replacement = prepareBlock(block as PMJson, blockId);
        const candidate = replaceAt(current, [...path], replacement);
        const checked = validateDocument(canonicalize(candidate), this.config.limits);
        if (!checked.ok) {
          const first = checked.errors[0];
          return { status: 422, body: { error: "invalid_change", code: first?.code ?? "invalid", path: first?.path ?? "/" } };
        }

        // 3. rehearse on a copy: the real Yjs result must equal what was validated
        const copy = new Y.Doc();
        try {
          Y.applyUpdate(copy, Y.encodeStateAsUpdate(document));
          replaceInYjs(copy, blockId, replacement);
          const rehearsal = yDocToProsemirrorJSON(copy, YJS_FIELD) as PMJson;
          if ((await contentHash(rehearsal)) !== (await contentHash(checked.doc))) {
            this.log.error("ai apply rehearsal mismatch", { document: documentId });
            return { status: 500, body: { error: "apply_mismatch" } };
          }
        } finally {
          copy.destroy();
        }

        // 4. the real change: ONE synchronous transaction with its own origin. The target is compared
        //    once more inside it (no await can interleave with a collaborator's edit here).
        let raced = false;
        await direct.transact((live) => {
          const now = yDocToProsemirrorJSON(live, YJS_FIELD) as PMJson;
          const nowPath = findPath(now.content, blockId);
          if (!nowPath || canonicalStringify(canonicalize(nodeAt({ content: now.content } as PMJson, nowPath))) !== targetSnapshot) {
            raced = true;
            return;
          }
          replaceInYjs(live, blockId, replacement);
        });
        if (raced) return { status: 409, body: { error: "block_changed" } };

        const stored = await this.server.storeNow(name, document);
        const after = yDocToProsemirrorJSON(document, YJS_FIELD) as PMJson;
        this.log.info("ai apply", { document: documentId, block: blockId, bytes: Buffer.byteLength(blockText), counter: stored.counter });
        return {
          status: 200,
          body: {
            yjs_update_counter: stored.counter,
            content_hash: await contentHash(after),
            block_hash: await blockHash(replacement),
          },
        };
      });
    } catch (error) {
      this.log.error("ai apply failed", { document: documentId, error: error instanceof Error ? error.name : "error" });
      return { status: 503, body: { error: "unavailable" } };
    }
  }
}
