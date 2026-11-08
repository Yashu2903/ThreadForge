/**
 * Plain text of a document, for search and for the DOCX text-conservation check.
 * Blocks are separated by "\n", a hard break is "\n", table cells by "\t" and rows by "\n", list items
 * by "\n". Images contribute nothing (their alt text is metadata, not document text).
 */
import type { PMJson } from "./canonical.js";

export function extractText(doc: PMJson): string {
  const parts: string[] = [];
  const walk = (node: PMJson) => {
    switch (node.type) {
      case "text":
        parts.push(node.text ?? "");
        return;
      case "hardBreak":
        parts.push("\n");
        return;
      case "image":
        return;
      case "table":
        (node.content ?? []).forEach((row, r) => {
          if (r > 0) parts.push("\n");
          (row.content ?? []).forEach((cell, c) => {
            if (c > 0) parts.push("\t");
            (cell.content ?? []).forEach((p, i) => {
              if (i > 0) parts.push("\n");
              walk(p);
            });
          });
        });
        return;
      default: {
        const children = node.content ?? [];
        const blockLevel = new Set(["doc", "blockquote", "bulletList", "orderedList", "listItem"]);
        children.forEach((child, i) => {
          if (i > 0 && (blockLevel.has(node.type ?? "") || false)) parts.push("\n");
          walk(child);
        });
      }
    }
  };
  walk(doc);
  return parts.join("");
}

/** Collapse all whitespace runs to one space and trim: the comparison form for text-conservation checks. */
export function normalizeWhitespace(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}
