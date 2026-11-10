/**
 * Applying the ENGINE'S RESULT to a live Yjs document.
 *
 * The shared engine (`applyOperations`, packages/editor-schema/src/ops.ts) computes, on plain ProseMirror JSON,
 * the candidate document and a list of `changes` (for each operation: the block(s) before and after, with their
 * ids). This module performs exactly those changes on the shared Y.XmlFragment, in ONE caller-provided
 * transaction. It makes no decisions of its own: block ids, new content and order all come from the engine, and
 * the caller proves afterwards (on a copy, before touching the live document) that the Yjs result equals the
 * engine's candidate.
 *
 * Replacing text keeps the block ELEMENT (its id, alignment, spacing, indent): only its inline children are
 * deleted and re-inserted. Only `delete_blocks` removes elements. New blocks are built by the library the editor
 * itself uses (`prosemirrorJSONToYXmlFragment`), so they have exactly the shape an editor would produce.
 */
import { type Change, type PMJson, YJS_FIELD, getEditorSchema } from "@threadforge/editor-schema";
import { prosemirrorJSONToYXmlFragment } from "@tiptap/y-tiptap";
import * as Y from "yjs";

/** The name of the root Y.Map that remembers which proposals were applied (the idempotency marker). */
export const MARKER_MAP = "ai_applications";
/** The `source` of the transaction origin of an AI apply: people's edits never carry it. */
export const AI_ORIGIN_SOURCE = "ai-apply";

type Found = { parent: Y.XmlFragment | Y.XmlElement; index: number; element: Y.XmlElement };

function find(parent: Y.XmlFragment | Y.XmlElement, id: string): Found | null {
  const children = parent.toArray();
  for (let i = 0; i < children.length; i += 1) {
    const child = children[i];
    if (!(child instanceof Y.XmlElement)) continue;
    if (child.getAttribute("id") === id) return { parent, index: i, element: child };
    const inner = find(child, id);
    if (inner) return inner;
  }
  return null;
}

/** Detached copies of the Y nodes an editor would create for these blocks. */
function blocksToY(blocks: PMJson[]): Y.XmlElement[] {
  const scratch = new Y.Doc();
  try {
    const fragment = scratch.getXmlFragment("x");
    prosemirrorJSONToYXmlFragment(getEditorSchema(), { type: "doc", content: blocks }, fragment);
    return fragment.toArray().map((n) => (n as Y.XmlElement).clone());
  } finally {
    scratch.destroy();
  }
}

/**
 * Detached copies of the inline children (text runs, hard breaks, images) of a text block.
 * The children are read from the INTEGRATED element inside the scratch document: a detached clone keeps its
 * children as "preliminary content", which `toArray()` does not return.
 */
function inlineToY(block: PMJson): (Y.XmlText | Y.XmlElement)[] {
  const shell: PMJson = { ...block, attrs: { ...(block.attrs ?? {}) } };
  const scratch = new Y.Doc();
  try {
    const fragment = scratch.getXmlFragment("x");
    prosemirrorJSONToYXmlFragment(getEditorSchema(), { type: "doc", content: [shell] }, fragment);
    const element = fragment.get(0);
    if (!(element instanceof Y.XmlElement)) return [];
    return element.toArray().map((n) => n.clone() as Y.XmlText | Y.XmlElement);
  } finally {
    scratch.destroy();
  }
}

export class ApplyError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "ApplyError";
  }
}

/** Perform `changes` on `doc`. MUST run inside one `doc.transact`; throws ApplyError if a target vanished. */
export function applyChangesToYjs(doc: Y.Doc, changes: readonly Change[]): void {
  const fragment = doc.getXmlFragment(YJS_FIELD);
  const insertedAfter = new Map<string, number>();
  for (const change of changes) {
    switch (change.kind) {
      case "replace_block_text":
      case "replace_range": {
        const hit = change.block_id ? find(fragment, change.block_id) : null;
        const after = change.after[0];
        if (!hit || !after) throw new ApplyError("block_not_found");
        hit.element.delete(0, hit.element.length);
        const children = inlineToY(after);
        if (children.length > 0) hit.element.insert(0, children);
        break;
      }
      case "insert_blocks_after": {
        const anchor = change.anchor ?? "";
        let position: number;
        if (anchor === "start") position = 0;
        else {
          const index = fragment.toArray().findIndex((n) => n instanceof Y.XmlElement && n.getAttribute("id") === anchor);
          if (index === -1) throw new ApplyError("anchor_not_found");
          position = index + 1;
        }
        position += insertedAfter.get(anchor) ?? 0;
        const created = blocksToY(change.after);
        fragment.insert(position, created);
        insertedAfter.set(anchor, (insertedAfter.get(anchor) ?? 0) + created.length);
        break;
      }
      case "delete_blocks": {
        const hit = change.block_id ? find(fragment, change.block_id) : null;
        if (!hit) throw new ApplyError("block_not_found");
        hit.parent.delete(hit.index, 1);
        break;
      }
    }
  }
}
