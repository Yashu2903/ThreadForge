/**
 * What the toolbar shows for the current selection, read from the ProseMirror state.
 *
 * Tri-state: for a mark (bold...) or an attribute (font, size, colour...) the answer is "on"/a value
 * when EVERY piece of selected text has it, "off"/null when NONE has it, and "mixed" when it differs.
 * With a cursor (empty selection) the answer is about the text typed next (stored marks, else the
 * marks at the cursor).
 */
import type { Editor } from "@tiptap/core";
import type { Mark, Node as PMNode } from "@tiptap/pm/model";
import { NodeSelection } from "@tiptap/pm/state";

export type Tri = "on" | "off" | "mixed";
export type Mixed<T> = T | "mixed";

export interface FormatState {
  editable: boolean;
  bold: Tri;
  italic: Tri;
  underline: Tri;
  strike: Tri;
  script: Mixed<"superscript" | "subscript" | null>;
  font: Mixed<string | null>;
  size: Mixed<number | null>;
  color: Mixed<string | null>;
  highlight: Mixed<string | null>;
  link: { href: string } | null;
  block: Mixed<"paragraph" | "h1" | "h2" | "h3">;
  quote: boolean;
  align: Mixed<string>;
  lineSpacing: Mixed<number | null>;
  spaceBefore: Mixed<number | null>;
  spaceAfter: Mixed<number | null>;
  list: { ordered: boolean } | null;
  table: { rows: number; cols: number } | null;
  image: { alt: string } | null;
  canUndo: boolean;
  canRedo: boolean;
}

function marksOfSelection(editor: Editor): (readonly Mark[])[] {
  const { state } = editor;
  const { selection } = state;
  if (selection.empty) {
    return [state.storedMarks ?? selection.$from.marks()];
  }
  const out: (readonly Mark[])[] = [];
  for (const range of selection.ranges) {
    state.doc.nodesBetween(range.$from.pos, range.$to.pos, (node) => {
      if (node.isText) out.push(node.marks);
      else if (node.type.name === "image") out.push(node.marks);
    });
  }
  return out.length ? out : [[]];
}

function tri(sets: (readonly Mark[])[], name: string): Tri {
  const hits = sets.filter((marks) => marks.some((m) => m.type.name === name)).length;
  return hits === 0 ? "off" : hits === sets.length ? "on" : "mixed";
}

function distinct<T>(sets: (readonly Mark[])[], markName: string, attr: string | null, normalize: (v: unknown) => T): Mixed<T> {
  const values = new Set<T>();
  for (const marks of sets) {
    const mark = marks.find((m) => m.type.name === markName);
    values.add(normalize(mark && attr ? mark.attrs[attr] : mark ? true : null));
  }
  const all = [...values];
  return all.length === 1 ? (all[0] as T) : "mixed";
}

const orNull = <T,>(v: unknown): T | null => (v === undefined || v === null || v === "" ? null : (v as T));

function textblocks(editor: Editor): PMNode[] {
  const { state } = editor;
  const found: PMNode[] = [];
  for (const range of state.selection.ranges) {
    state.doc.nodesBetween(range.$from.pos, range.$to.pos, (node) => {
      if (node.isTextblock) found.push(node);
    });
  }
  if (!found.length) found.push(state.selection.$from.parent);
  return found;
}

function blockKind(node: PMNode): "paragraph" | "h1" | "h2" | "h3" {
  if (node.type.name === "heading") return (`h${Math.min(3, Math.max(1, Number(node.attrs.level) || 1))}`) as "h1" | "h2" | "h3";
  return "paragraph";
}

function distinctBlock<T>(blocks: PMNode[], read: (n: PMNode) => T): Mixed<T> {
  const values = new Set(blocks.map(read));
  const all = [...values];
  return all.length === 1 ? (all[0] as T) : "mixed";
}

function ancestor(editor: Editor, names: string[]): { node: PMNode; depth: number } | null {
  const { $from } = editor.state.selection;
  for (let depth = $from.depth; depth > 0; depth--) {
    const node = $from.node(depth);
    if (names.includes(node.type.name)) return { node, depth };
  }
  return null;
}

export function readFormatState(editor: Editor): FormatState {
  const sets = marksOfSelection(editor);
  const blocks = textblocks(editor);
  const { selection } = editor.state;

  const scriptSup = tri(sets, "superscript");
  const scriptSub = tri(sets, "subscript");
  const script: FormatState["script"] =
    scriptSup === "on" ? "superscript" : scriptSub === "on" ? "subscript" : scriptSup === "mixed" || scriptSub === "mixed" ? "mixed" : null;

  const linkMark = sets[0]?.find((m) => m.type.name === "link");
  const list = ancestor(editor, ["bulletList", "orderedList"]);
  const table = ancestor(editor, ["table"]);
  const quote = blocks.length > 0 && ancestor(editor, ["blockquote"]) !== null;

  const image =
    selection instanceof NodeSelection && selection.node.type.name === "image" ? { alt: String(selection.node.attrs.alt ?? "") } : null;

  return {
    editable: editor.isEditable,
    bold: tri(sets, "bold"),
    italic: tri(sets, "italic"),
    underline: tri(sets, "underline"),
    strike: tri(sets, "strike"),
    script,
    font: distinct<string | null>(sets, "textStyle", "fontKey", orNull),
    size: distinct<number | null>(sets, "textStyle", "fontSizePt", orNull),
    color: distinct<string | null>(sets, "textStyle", "color", orNull),
    highlight: distinct<string | null>(sets, "highlight", "color", orNull),
    link: linkMark ? { href: String(linkMark.attrs.href ?? "") } : null,
    block: distinctBlock(blocks, blockKind),
    quote,
    align: distinctBlock(blocks, (n) => String(n.attrs.textAlign ?? "left")),
    lineSpacing: distinctBlock(blocks, (n) => orNull<number>(n.attrs.lineSpacing)),
    spaceBefore: distinctBlock(blocks, (n) => orNull<number>(n.attrs.spaceBefore)),
    spaceAfter: distinctBlock(blocks, (n) => orNull<number>(n.attrs.spaceAfter)),
    list: list ? { ordered: list.node.type.name === "orderedList" } : null,
    table: table ? { rows: table.node.childCount, cols: table.node.firstChild?.childCount ?? 0 } : null,
    image,
    canUndo: editor.can().undo(),
    canRedo: editor.can().redo(),
  };
}
