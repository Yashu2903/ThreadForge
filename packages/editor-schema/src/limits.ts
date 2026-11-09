/**
 * Document limits and the font allowlist. One definition for the browser editor, the collaboration
 * service and the tests. Exceeding a limit is an error with a path; nothing is ever truncated.
 */

export const FONT_KEYS = [
  "sans",
  "serif",
  "mono",
  "calibri",
  "cambria",
  "arial",
  "times_new_roman",
  "courier_new",
] as const;

export interface Limits {
  /** UTF-8 bytes of the canonical JSON */
  maxSerializedBytes: number;
  maxTextCodepoints: number;
  /** block nodes anywhere (paragraph, heading, blockquote, lists, table) plus list items */
  maxBlocks: number;
  /** every node, including text nodes, rows and cells */
  maxTotalNodes: number;
  maxImages: number;
  /** diagram blocks in one document (each is also bounded by the Mermaid source limits) */
  maxDiagrams: number;
  maxListDepth: number;
  maxLinkLength: number;
  maxAltCodepoints: number;
  maxTableRows: number;
  maxTableCols: number;
  maxImagePx: number;
  allowedFonts: readonly string[];
}

export const DEFAULT_LIMITS: Limits = {
  maxSerializedBytes: 1024 * 1024,
  maxTextCodepoints: 100_000,
  maxBlocks: 5_000,
  maxTotalNodes: 60_000,
  maxImages: 100,
  maxDiagrams: 25,
  maxListDepth: 3,
  maxLinkLength: 2048,
  maxAltCodepoints: 500,
  maxTableRows: 50,
  maxTableCols: 20,
  maxImagePx: 10_000,
  allowedFonts: FONT_KEYS,
};

export const ALIGNMENTS = ["left", "center", "right", "justify"] as const;
export const LINE_SPACINGS = [1, 1.15, 1.5, 2] as const;
export const MAX_INDENT_LEVEL = 8;
export const INDENT_PT = 36;
export const MIN_FONT_PT = 8;
export const MAX_FONT_PT = 72;
export const MAX_SPACE_PT = 72;
