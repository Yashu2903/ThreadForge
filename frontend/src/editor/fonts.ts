import type { Node as PMNode } from "@tiptap/pm/model";

/** The font keys a document may use, what the picker shows, and what the browser draws them with. */
export interface FontOption {
  key: string;
  label: string;
  /** the openly licensed, metric-compatible font we actually ship for it (null: it is the real thing) */
  substitute: string | null;
}

export const FONT_OPTIONS: FontOption[] = [
  { key: "sans", label: "Sans serif (Arimo)", substitute: null },
  { key: "serif", label: "Serif (Tinos)", substitute: null },
  { key: "mono", label: "Monospace (Cousine)", substitute: null },
  { key: "calibri", label: "Calibri", substitute: "Carlito" },
  { key: "cambria", label: "Cambria", substitute: "Caladea" },
  { key: "arial", label: "Arial", substitute: "Arimo" },
  { key: "times_new_roman", label: "Times New Roman", substitute: "Tinos" },
  { key: "courier_new", label: "Courier New", substitute: "Cousine" },
];

export const SIZE_OPTIONS = [8, 9, 10, 10.5, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72];

/** Font keys that appear in a document (a ProseMirror node). */
export function fontsUsed(doc: PMNode): Set<string> {
  const found = new Set<string>();
  doc.descendants((node) => {
    for (const mark of node.marks) {
      if (mark.type.name === "textStyle" && mark.attrs.fontKey) found.add(String(mark.attrs.fontKey));
    }
  });
  return found;
}

/**
 * A notice when the document uses a font we draw with a substitute (Calibri -> Carlito, ...). The
 * substitutes have the same character widths, so line breaks match, but the glyph shapes differ and
 * a machine with the real font will show the real one. Null when nothing needs saying.
 */
export function fontNotice(doc: PMNode): string | null {
  const used = [...fontsUsed(doc)].map((k) => FONT_OPTIONS.find((o) => o.key === k)).filter((o): o is FontOption => !!o && !!o.substitute);
  if (used.length === 0) return null;
  const list = used.map((o) => `${o.label} is shown as ${o.substitute}`).join("; ");
  return `${list}. Spacing matches, but the letter shapes can look different from the original, and the document may look different on another computer.`;
}
