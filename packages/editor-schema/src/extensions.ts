/**
 * The ONE extension list. The browser editor, the collaboration service and the tests all build
 * their ProseMirror schema from createExtensions(); if two sides used different schemas, content
 * would be silently dropped when one side loads JSON the other wrote.
 *
 * Standard open-source Tiptap extensions used as they are (MIT, tiptap v3): StarterKit (document,
 * paragraph, text, heading, hard break, bold, italic, strike, underline, bullet/ordered list,
 * blockquote, gapcursor, dropcursor, list keymap), TextStyle, Highlight, Subscript, Superscript,
 * TextAlign, Table/TableRow/TableCell/TableHeader, UniqueID.
 *
 * Standard extensions CONFIGURED or EXTENDED (what we changed and why):
 *   StarterKit      undoRedo off (Yjs provides undo), code/codeBlock/horizontalRule/trailingNode off,
 *                   headings 1-3, blockquote/listItem/link/underline replaced by restricted versions
 *   Blockquote      content restricted to paragraphs (the old model: quotes hold paragraphs only)
 *   ListItem        a paragraph first, then paragraphs or lists (no headings/quotes/tables inside)
 *   Link            ONLY href is stored; scheme policy at paste/parse time; target/rel are render-only
 *   Highlight       multicolor, colour normalised to #RRGGBB, default yellow (never a bare <mark>)
 *   Superscript/Subscript  mutually exclusive (ProseMirror mark `excludes`)
 *   TableCell/Header content restricted to paragraphs; merged cells are rejected by validateDocument
 *
 * Extensions we had to WRITE (no standard equivalent with our constraints):
 *   ParagraphFormat   lineSpacing, spaceBefore, spaceAfter, indent as validated attributes
 *   TextFormat        fontKey (allowlist key, never CSS), fontSizePt (8-72), color (#RRGGBB) on textStyle
 *   AssetImage        inline image node with assetId (no stored src; URL derived from the room endpoint)
 */
import { Extension, Node as TipNode, mergeAttributes, type AnyExtension } from "@tiptap/core";
import { Blockquote } from "@tiptap/extension-blockquote";
import Highlight from "@tiptap/extension-highlight";
import { Link } from "@tiptap/extension-link";
import { ListItem } from "@tiptap/extension-list";
import Subscript from "@tiptap/extension-subscript";
import Superscript from "@tiptap/extension-superscript";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import TextAlign from "@tiptap/extension-text-align";
import { TextStyle } from "@tiptap/extension-text-style";
import UniqueID from "@tiptap/extension-unique-id";
import StarterKit from "@tiptap/starter-kit";
import { normalizeColor } from "./colors.js";
import { ALIGNMENTS, FONT_KEYS, INDENT_PT, MAX_FONT_PT, MIN_FONT_PT } from "./limits.js";
import { checkLink } from "./links.js";

/** Node types that carry a stable block id. */
export const ID_TYPES = [
  "paragraph",
  "heading",
  "blockquote",
  "bulletList",
  "orderedList",
  "listItem",
  "table",
  "tableRow",
  "tableCell",
  "tableHeader",
] as const;

const number = (value: string | null | undefined): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/** lineSpacing, spaceBefore, spaceAfter, indent: validated attributes on paragraphs and headings. */
export const ParagraphFormat = Extension.create({
  name: "paragraphFormat",
  addGlobalAttributes() {
    return [
      {
        types: ["paragraph", "heading"],
        attributes: {
          lineSpacing: {
            default: null,
            parseHTML: (el: HTMLElement) => number(el.getAttribute("data-line-spacing")),
            renderHTML: (attrs: Record<string, unknown>) =>
              attrs.lineSpacing ? { "data-line-spacing": String(attrs.lineSpacing), style: `line-height:${attrs.lineSpacing}` } : {},
          },
          spaceBefore: {
            default: null,
            parseHTML: (el: HTMLElement) => number(el.getAttribute("data-space-before")),
            renderHTML: (attrs: Record<string, unknown>) =>
              attrs.spaceBefore ? { "data-space-before": String(attrs.spaceBefore), style: `margin-top:${attrs.spaceBefore}pt` } : {},
          },
          spaceAfter: {
            default: null,
            parseHTML: (el: HTMLElement) => number(el.getAttribute("data-space-after")),
            renderHTML: (attrs: Record<string, unknown>) =>
              attrs.spaceAfter ? { "data-space-after": String(attrs.spaceAfter), style: `margin-bottom:${attrs.spaceAfter}pt` } : {},
          },
          indent: {
            default: null,
            parseHTML: (el: HTMLElement) => number(el.getAttribute("data-indent")),
            renderHTML: (attrs: Record<string, unknown>) =>
              attrs.indent ? { "data-indent": String(attrs.indent), style: `margin-left:${Number(attrs.indent) * INDENT_PT}pt` } : {},
          },
        },
      },
    ];
  },
});

/** fontKey, fontSizePt and color live on the standard `textStyle` mark. */
export const TextFormat = Extension.create({
  name: "textFormat",
  addGlobalAttributes() {
    return [
      {
        types: ["textStyle"],
        attributes: {
          fontKey: {
            default: null,
            parseHTML: (el: HTMLElement) => {
              const key = (el.getAttribute("data-font-key") ?? "").trim();
              return (FONT_KEYS as readonly string[]).includes(key) ? key : null;
            },
            renderHTML: (attrs: Record<string, unknown>) => (attrs.fontKey ? { "data-font-key": String(attrs.fontKey), class: `font-${attrs.fontKey}` } : {}),
          },
          fontSizePt: {
            default: null,
            parseHTML: (el: HTMLElement) => {
              const n = number(el.getAttribute("data-font-size-pt"));
              return n !== null && n >= MIN_FONT_PT && n <= MAX_FONT_PT && Number.isInteger(n * 2) ? n : null;
            },
            renderHTML: (attrs: Record<string, unknown>) => (attrs.fontSizePt ? { "data-font-size-pt": String(attrs.fontSizePt), style: `font-size:${attrs.fontSizePt}pt` } : {}),
          },
          color: {
            default: null,
            parseHTML: (el: HTMLElement) => normalizeColor(el.getAttribute("data-color") ?? el.style.color),
            renderHTML: (attrs: Record<string, unknown>) => (attrs.color ? { "data-color": String(attrs.color), style: `color:${attrs.color}` } : {}),
          },
        },
      },
    ];
  },
});

export interface AssetImageOptions {
  /** Where the browser fetches an asset: derived, never stored in the document. */
  assetUrl: (assetId: string) => string;
}

/** An inline image that references an uploaded asset by id. No src is stored; pasted/remote/base64 images do not parse. */
export const AssetImage = TipNode.create<AssetImageOptions>({
  name: "image",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,
  addOptions() {
    return { assetUrl: (assetId: string) => `/assets/${assetId}` };
  },
  addAttributes() {
    return {
      assetId: { default: null },
      alt: { default: "" },
      width: { default: null },
      height: { default: null },
    };
  },
  parseHTML() {
    return [
      {
        // only OUR rendering of an image parses back: <img src="https://..."> and data: URLs are dropped
        tag: "img[data-asset-id]",
        getAttrs: (el: HTMLElement | string) => {
          if (typeof el === "string") return false;
          return {
            assetId: el.getAttribute("data-asset-id"),
            alt: el.getAttribute("alt") ?? "",
            width: number(el.getAttribute("width")),
            height: number(el.getAttribute("height")),
          };
        },
      },
    ];
  },
  renderHTML({ node, HTMLAttributes }) {
    const id = String(node.attrs.assetId ?? "");
    return [
      "img",
      mergeAttributes(HTMLAttributes, {
        "data-asset-id": id,
        src: id ? this.options.assetUrl(id) : "",
        alt: node.attrs.alt ?? "",
        width: node.attrs.width ?? undefined,
        height: node.attrs.height ?? undefined,
        draggable: "false",
        loading: "lazy",
      }),
    ];
  },
  addCommands() {
    return {
      setAssetImage:
        (attrs: { assetId: string; alt?: string; width?: number; height?: number }) =>
        ({ commands }: { commands: { insertContent: (c: unknown) => boolean } }) =>
          commands.insertContent({ type: this.name, attrs }),
    } as never;
  },
});

export interface CreateExtensionsOptions {
  assetUrl?: (assetId: string) => string;
  /** passed to UniqueID: ignore transactions that came from other collaborators (isChangeOrigin) */
  uniqueIdFilter?: (transaction: never) => boolean;
  /** generate ids (default crypto.randomUUID) */
  generateId?: () => string;
}

export function createExtensions(options: CreateExtensionsOptions = {}): AnyExtension[] {
  const generateId = options.generateId ?? (() => globalThis.crypto.randomUUID());
  return [
    StarterKit.configure({
      undoRedo: false, // Yjs provides undo/redo (Tiptap docs: "Disable the UndoRedo extension")
      code: false,
      codeBlock: false,
      horizontalRule: false,
      trailingNode: false,
      blockquote: false,
      listItem: false,
      link: false,
      heading: { levels: [1, 2, 3] },
    }),
    Blockquote.extend({ content: "paragraph+" }),
    ListItem.extend({ content: "paragraph (paragraph | bulletList | orderedList)*" }),
    Link.extend({
      addAttributes() {
        return { href: { default: null } }; // target/rel are render-time only (HTMLAttributes below)
      },
    }).configure({
      openOnClick: false,
      autolink: false,
      linkOnPaste: false,
      isAllowedUri: (uri: string) => checkLink(uri) !== null,
      HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" },
    }),
    TextStyle,
    TextFormat,
    Highlight.extend({
      addAttributes() {
        return {
          color: {
            default: "#FFFF00",
            parseHTML: (el: HTMLElement) => normalizeColor(el.getAttribute("data-color") ?? el.style.backgroundColor) ?? "#FFFF00",
            renderHTML: (attrs: Record<string, unknown>) => ({ "data-color": String(attrs.color), style: `background-color:${attrs.color}` }),
          },
        };
      },
    }).configure({ multicolor: true }),
    Superscript.extend({ excludes: "superscript subscript" }),
    Subscript.extend({ excludes: "superscript subscript" }),
    TextAlign.configure({ types: ["heading", "paragraph"], alignments: [...ALIGNMENTS], defaultAlignment: "left" }),
    ParagraphFormat,
    Table.configure({ resizable: false }),
    TableRow,
    TableHeader.extend({ content: "paragraph+" }),
    TableCell.extend({ content: "paragraph+" }),
    AssetImage.configure({ assetUrl: options.assetUrl ?? ((id: string) => `/assets/${id}`) }),
    UniqueID.configure({
      types: [...ID_TYPES],
      attributeName: "id",
      generateID: generateId,
      ...(options.uniqueIdFilter ? { filterTransaction: options.uniqueIdFilter as never } : {}),
    }),
  ];
}
