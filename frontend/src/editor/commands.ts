/**
 * Every toolbar action, as one Tiptap command each. Nothing here touches the DOM and nothing writes
 * to the server: a command is a ProseMirror transaction; the Collaboration extension turns it into a
 * Yjs update and the provider sends it.
 */
import { checkLink, MAX_INDENT_LEVEL } from "@threadforge/editor-schema";
import type { Editor } from "@tiptap/core";

export type BlockKind = "paragraph" | "h1" | "h2" | "h3";

export const setBlock = (editor: Editor, kind: BlockKind): boolean =>
  kind === "paragraph"
    ? editor.chain().focus().setParagraph().run()
    : editor.chain().focus().setHeading({ level: Number(kind.slice(1)) as 1 | 2 | 3 }).run();

/** font / size / colour live as attributes of the `textStyle` mark; null clears one. */
export function setTextStyle(editor: Editor, attr: "fontKey" | "fontSizePt" | "color", value: string | number | null): boolean {
  return editor.chain().focus().setMark("textStyle", { [attr]: value }).removeEmptyTextStyle().run();
}

export function setScript(editor: Editor, value: "superscript" | "subscript" | null): boolean {
  const chain = editor.chain().focus();
  if (value === "superscript") return chain.unsetSubscript().toggleSuperscript().run();
  if (value === "subscript") return chain.unsetSuperscript().toggleSubscript().run();
  return chain.unsetSuperscript().unsetSubscript().run();
}

export function setHighlight(editor: Editor, color: string | null): boolean {
  return color ? editor.chain().focus().setHighlight({ color }).run() : editor.chain().focus().unsetHighlight().run();
}

export function clearColors(editor: Editor): boolean {
  return editor.chain().focus().setMark("textStyle", { color: null }).removeEmptyTextStyle().unsetHighlight().run();
}

/** Paragraph/heading attributes (lineSpacing, spaceBefore, spaceAfter, indent) on every selected block. */
export function setBlockAttrs(editor: Editor, attrs: Record<string, number | null>): boolean {
  return editor.chain().focus().updateAttributes("paragraph", attrs).updateAttributes("heading", attrs).run();
}

export function setAlign(editor: Editor, align: "left" | "center" | "right" | "justify"): boolean {
  return editor.chain().focus().setTextAlign(align).run();
}

/** Indent: list items nest (sink/lift); everything else moves the block's indent level. */
export function changeIndent(editor: Editor, delta: 1 | -1): boolean {
  if (editor.isActive("listItem")) {
    return delta === 1 ? editor.chain().focus().sinkListItem("listItem").run() : editor.chain().focus().liftListItem("listItem").run();
  }
  return editor
    .chain()
    .focus()
    .command(({ tr, state }) => {
      const { from, to } = state.selection;
      state.doc.nodesBetween(from, to, (node, pos) => {
        if (node.type.name !== "paragraph" && node.type.name !== "heading") return;
        const next = Math.min(MAX_INDENT_LEVEL, Math.max(0, (Number(node.attrs.indent) || 0) + delta));
        tr.setNodeMarkup(pos, undefined, { ...node.attrs, indent: next === 0 ? null : next });
      });
      return true;
    })
    .run();
}

/**
 * Apply a link. `href` is checked with the same policy the server enforces (http, https, mailto);
 * a rejected address is not applied and the function says so. With a cursor and no link under it the
 * address itself becomes the link text.
 */
export function applyLink(editor: Editor, href: string): { ok: true } | { ok: false; message: string } {
  const normalized = checkLink(href);
  if (!normalized) return { ok: false, message: "Use an address that starts with http://, https:// or mailto:." };
  if (editor.state.selection.empty && !editor.isActive("link")) {
    editor
      .chain()
      .focus()
      .insertContent({ type: "text", text: normalized, marks: [{ type: "link", attrs: { href: normalized } }] })
      .run();
  } else {
    editor.chain().focus().extendMarkRange("link").setLink({ href: normalized }).run();
  }
  return { ok: true };
}

export const removeLink = (editor: Editor): boolean => editor.chain().focus().extendMarkRange("link").unsetLink().run();

export const insertAssetImage = (editor: Editor, asset: { id: string; width_px: number; height_px: number }): boolean =>
  editor
    .chain()
    .focus()
    .insertContent({ type: "image", attrs: { assetId: asset.id, alt: "", width: asset.width_px, height: asset.height_px } })
    .run();

export const setImageAlt = (editor: Editor, alt: string): boolean => editor.chain().focus().updateAttributes("image", { alt: alt.slice(0, 500) }).run();

export const insertTable = (editor: Editor, rows: number, cols: number, header: boolean): boolean =>
  editor
    .chain()
    .focus()
    .insertTable({ rows: Math.min(50, Math.max(1, rows)), cols: Math.min(20, Math.max(1, cols)), withHeaderRow: header })
    .run();

/** Only structure-preserving table commands exist here: merge/split are deliberately not exposed
 *  (merged cells are rejected by the shared validator). */
export const tableCommand = {
  rowAbove: (e: Editor) => e.chain().focus().addRowBefore().run(),
  rowBelow: (e: Editor) => e.chain().focus().addRowAfter().run(),
  colLeft: (e: Editor) => e.chain().focus().addColumnBefore().run(),
  colRight: (e: Editor) => e.chain().focus().addColumnAfter().run(),
  deleteRow: (e: Editor) => e.chain().focus().deleteRow().run(),
  deleteColumn: (e: Editor) => e.chain().focus().deleteColumn().run(),
  deleteTable: (e: Editor) => e.chain().focus().deleteTable().run(),
};
