/**
 * The editor's view of the document: an immutable tree (the canonical schema, see
 * docs/document-schema.md) plus an id index.
 *
 * IMMUTABLE with structural sharing: every edit returns a NEW root in which only the nodes on the
 * path from the root to the changed node are new objects; everything else is the same object. That
 * makes "did this block change?" a pointer comparison (the renderer relies on it), and makes undo
 * snapshots cheap (a snapshot is just a root pointer).
 *
 * Block ids are unique across the whole document, so a position names its text block by id alone;
 * the "block id path" of the spec is derived from the index (`chain`) when needed.
 */
import type {
  Block,
  DocumentContent,
  Heading,
  Inline,
  ListItem,
  Paragraph,
  TableCell,
  TableRow,
} from "threadforge-client/document-schema";

export type Doc = DocumentContent;
export type Node = Block | ListItem | TableRow | TableCell;
export type TextBlock = Paragraph | Heading;

export const isTextBlock = (n: { type: string }): n is TextBlock => n.type === "paragraph" || n.type === "heading";

/** The children of a container node (quote, list, list_item, table, row, cell); undefined for text blocks. */
export function childrenOf(n: Node): Node[] | undefined {
  return isTextBlock(n) ? undefined : (n.children as Node[]);
}

export interface DocIndex {
  nodes: Map<string, Node>;
  parent: Map<string, string | null>; // null: a top-level block
  /** text block ids in document order */
  order: string[];
  orderIdx: Map<string, number>;
  chain(id: string): string[]; // ids from the top-level block down to `id`
}

const cache = new WeakMap<Doc, DocIndex>();

export function indexOf(doc: Doc): DocIndex {
  const hit = cache.get(doc);
  if (hit) return hit;
  const nodes = new Map<string, Node>();
  const parent = new Map<string, string | null>();
  const order: string[] = [];
  const walk = (node: Node, parentId: string | null) => {
    nodes.set(node.id, node);
    parent.set(node.id, parentId);
    if (isTextBlock(node)) {
      order.push(node.id);
      return;
    }
    for (const child of node.children as Node[]) walk(child, node.id);
  };
  for (const block of doc.blocks) walk(block, null);
  const orderIdx = new Map(order.map((id, i) => [id, i]));
  const index: DocIndex = {
    nodes,
    parent,
    order,
    orderIdx,
    chain(id) {
      const out: string[] = [];
      for (let cur: string | null | undefined = id; cur; cur = parent.get(cur)) out.unshift(cur);
      return out;
    },
  };
  cache.set(doc, index);
  return index;
}

export function nodeById(doc: Doc, id: string): Node | undefined {
  return indexOf(doc).nodes.get(id);
}

export function textBlock(doc: Doc, id: string): TextBlock {
  const node = nodeById(doc, id);
  if (!node || !isTextBlock(node)) throw new Error(`not a text block: ${id}`);
  return node;
}

export function parentOf(doc: Doc, id: string): Node | null {
  const p = indexOf(doc).parent.get(id);
  return p ? (nodeById(doc, p) ?? null) : null;
}

/** The nearest ancestor table_cell of a text block, or null. */
export function cellOf(doc: Doc, id: string): string | null {
  const idx = indexOf(doc);
  for (let cur = idx.parent.get(id); cur; cur = idx.parent.get(cur)) {
    if (idx.nodes.get(cur)!.type === "table_cell") return cur;
  }
  return null;
}

export function inlineLength(children: readonly Inline[] | undefined): number {
  if (!children) return 0;
  let n = 0;
  for (const c of children) n += c.type === "text" ? c.text.length : 1;
  return n;
}

export const blockLength = (b: TextBlock): number => inlineLength(b.children);

/**
 * Replace/remove/expand the node `id`. `fn` returns the replacement node, an array of nodes, or null
 * to delete it. Only the path root -> id is copied.
 */
export function updateNode(doc: Doc, id: string, fn: (node: Node) => Node | Node[] | null): Doc {
  const chain = indexOf(doc).chain(id);
  if (chain.length === 0) throw new Error(`unknown node ${id}`);
  const rec = (siblings: readonly Node[], depth: number): readonly Node[] => {
    const target = chain[depth]!;
    const out: Node[] = [];
    let changed = false;
    for (const n of siblings) {
      if (n.id !== target) {
        out.push(n);
        continue;
      }
      if (depth === chain.length - 1) {
        const r = fn(n);
        if (r === null) changed = true;
        else if (Array.isArray(r)) {
          out.push(...r);
          changed = true;
        } else {
          out.push(r);
          if (r !== n) changed = true;
        }
      } else {
        const kids = childrenOf(n)!;
        const next = rec(kids, depth + 1);
        if (next === kids) out.push(n);
        else {
          out.push({ ...n, children: next } as Node);
          changed = true;
        }
      }
    }
    return changed ? out : siblings;
  };
  const blocks = rec(doc.blocks as Node[], 0) as Block[];
  return blocks === doc.blocks ? doc : { ...doc, blocks };
}

export function newId(): string {
  return crypto.randomUUID();
}

/** Set a text block's inline children, keeping the canonical rule that an empty block has none. */
export function withInlines<T extends TextBlock>(block: T, inlines: Inline[]): T {
  if (inlines.length === 0) {
    const { children: _drop, ...rest } = block;
    return rest as T;
  }
  return { ...block, children: inlines };
}

/**
 * Remove a node and then clean up what that leaves behind: a container with no children is removed
 * too, a list item must start with a paragraph (an empty one is inserted if needed), a cell keeps at
 * least one paragraph, and the document keeps at least one block.
 */
export function removeNode(doc: Doc, id: string): Doc {
  const chain = indexOf(doc).chain(id);
  const rec = (siblings: readonly Node[], depth: number): readonly Node[] => {
    const target = chain[depth]!;
    const out: Node[] = [];
    let changed = false;
    for (const n of siblings) {
      if (n.id !== target) {
        out.push(n);
        continue;
      }
      changed = true;
      if (depth === chain.length - 1) continue;
      const kids = rec(childrenOf(n)!, depth + 1) as Node[];
      if (kids.length === 0) {
        if (n.type === "table_cell") out.push({ ...n, children: [{ type: "paragraph", id: newId() }] } as Node);
        continue; // an emptied list / list item / quote / row disappears
      }
      if (n.type === "list_item" && kids[0]!.type !== "paragraph") {
        out.push({ ...n, children: [{ type: "paragraph", id: newId() }, ...kids] } as Node);
      } else out.push({ ...n, children: kids } as Node);
    }
    return changed ? out : siblings;
  };
  let blocks = rec(doc.blocks as Node[], 0) as Block[];
  if (blocks.length === 0) blocks = [{ type: "paragraph", id: newId() }];
  return { ...doc, blocks };
}
