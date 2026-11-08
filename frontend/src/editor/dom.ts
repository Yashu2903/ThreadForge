/**
 * The editor's DOM: rendering the model, mapping DOM points to model positions and back, and reading
 * a text block back out of the DOM.
 *
 * DOM -> MODEL MAPPING (the exact rules, relied on by tests):
 *   - every model node has an element with data-id = the node id (p, h1-h3, blockquote, ul/ol, li,
 *     table, tr, td/th). Text blocks (p, h1-h3) also carry data-kind="textblock".
 *   - inside a text block the DOM children correspond 1:1, in order, to the model's inline nodes:
 *       text node without marks   -> a DOM Text node
 *       text node with marks      -> a wrapper element (strong/em/u/s/sup/sub/span/a nested; the
 *                                    outermost carries data-m = canonical JSON of the marks) whose
 *                                    innermost child is the DOM Text node
 *       hard_break                -> <br data-atom="hard_break">
 *       image                     -> <img data-atom="image" data-id=... contenteditable="false">
 *     plus, when the block is empty or ends in a hard break, one <br data-filler> (zero length).
 *   - OFFSET of a point in a text block = UTF-16 units of the text before it + 1 per atom before it.
 *   - a point NOT inside a text block (e.g. between table rows) maps to the nearest text block: the
 *     start of the first one after it, else the end of the last one before it.
 *   - a point inside an image cannot exist (contenteditable=false); a point before/after one is
 *     (textblock element, child index).
 */
import type { Inline, Marks, TextNode } from "threadforge-client/document-schema";
import { canonicalJson, type Json } from "threadforge-client/document-schema";
import type { Pos } from "./commands";
import { sanitizeText } from "./commands";
import { type Doc, type Node, type TextBlock, isTextBlock } from "./doc";
import { normalizeInlines } from "./inline";

export interface RenderContext {
  assetUrl(assetId: string): string;
}

const marksKey = (m: Marks): string => canonicalJson(m as unknown as Json);

// ---- creating elements ---------------------------------------------------------------------------------------

function tagFor(node: Node): string {
  switch (node.type) {
    case "paragraph":
      return "p";
    case "heading":
      return `h${node.level}`;
    case "quote":
      return "blockquote";
    case "list":
      return node.ordered ? "ol" : "ul";
    case "list_item":
      return "li";
    case "table":
      return "table";
    case "table_row":
      return "tr";
    case "table_cell":
      return node.header ? "th" : "td";
  }
}

function paragraphStyle(node: TextBlock): string {
  const a = node.attrs;
  if (!a) return "";
  const parts: string[] = [];
  if (a.align) parts.push(`text-align:${a.align}`);
  if (a.line_spacing) parts.push(`line-height:${a.line_spacing}`);
  if (a.space_before_pt) parts.push(`margin-top:${a.space_before_pt}pt`);
  if (a.space_after_pt) parts.push(`margin-bottom:${a.space_after_pt}pt`);
  if (a.indent_level) parts.push(`margin-left:${a.indent_level * 36}pt`);
  return parts.join(";");
}

function createElementFor(node: Node): HTMLElement {
  const el = document.createElement(tagFor(node));
  el.dataset.id = node.id;
  if (isTextBlock(node)) el.dataset.kind = "textblock";
  if (node.type === "table") el.appendChild(document.createElement("tbody"));
  return el;
}

const isFiller = (n: ChildNode): boolean => n instanceof HTMLElement && n.dataset.filler !== undefined;
const isAtom = (n: ChildNode): boolean => n instanceof HTMLElement && n.dataset.atom !== undefined;

function wrapMarks(text: Text, marks: Marks): HTMLElement {
  let node: HTMLElement | Text = text;
  const wrap = (tag: string, apply?: (el: HTMLElement) => void) => {
    const el = document.createElement(tag);
    apply?.(el);
    el.appendChild(node);
    node = el;
  };
  if (marks.script === "superscript") wrap("sup");
  if (marks.script === "subscript") wrap("sub");
  if (marks.strike) wrap("s");
  if (marks.underline) wrap("u");
  if (marks.italic) wrap("em");
  if (marks.bold) wrap("strong");
  if (marks.font || marks.size_pt || marks.color || marks.highlight) {
    wrap("span", (el) => {
      if (marks.font) el.className = `font-${marks.font}`;
      if (marks.size_pt) el.style.fontSize = `${marks.size_pt}pt`;
      if (marks.color) el.style.color = marks.color;
      if (marks.highlight) el.style.backgroundColor = marks.highlight;
    });
  }
  if (marks.link) {
    wrap("a", (el) => {
      el.setAttribute("href", marks.link!.href);
      el.setAttribute("rel", "noopener noreferrer");
      el.setAttribute("target", "_blank");
    });
  }
  if ((node as globalThis.Node) === text) wrap("span"); // a mark we do not draw still needs its wrapper for data-m
  const top = node as unknown as HTMLElement;
  top.dataset.m = marksKey(marks);
  return top;
}

function createInline(inline: Inline, ctx: RenderContext): globalThis.Node {
  if (inline.type === "text") {
    const text = document.createTextNode(inline.text);
    return inline.marks && Object.keys(inline.marks).length > 0 ? wrapMarks(text, inline.marks) : text;
  }
  if (inline.type === "hard_break") {
    const br = document.createElement("br");
    br.dataset.atom = "hard_break";
    return br;
  }
  const img = document.createElement("img");
  img.dataset.atom = "image";
  img.dataset.id = inline.id;
  img.dataset.asset = inline.asset_id;
  img.setAttribute("contenteditable", "false");
  img.setAttribute("draggable", "false");
  img.setAttribute("src", ctx.assetUrl(inline.asset_id));
  img.setAttribute("alt", inline.alt ?? "");
  img.setAttribute("width", String(inline.width_px));
  img.setAttribute("height", String(inline.height_px));
  return img;
}

function innerText(el: globalThis.Node): Text | null {
  if (el.nodeType === 3) return el as Text;
  for (const child of Array.from(el.childNodes)) {
    const t = innerText(child);
    if (t) return t;
  }
  return null;
}

function inlineMatches(dom: ChildNode | undefined, inline: Inline): boolean {
  if (!dom) return false;
  if (inline.type === "text") {
    const marked = inline.marks && Object.keys(inline.marks).length > 0;
    if (!marked) return dom.nodeType === 3;
    return dom instanceof HTMLElement && dom.dataset.m === marksKey(inline.marks!) && innerText(dom) !== null;
  }
  if (inline.type === "hard_break") return dom instanceof HTMLElement && dom.dataset.atom === "hard_break";
  return (
    dom instanceof HTMLElement &&
    dom.dataset.atom === "image" &&
    dom.dataset.id === inline.id &&
    dom.dataset.asset === inline.asset_id &&
    dom.getAttribute("alt") === (inline.alt ?? "") &&
    dom.getAttribute("width") === String(inline.width_px) &&
    dom.getAttribute("height") === String(inline.height_px)
  );
}

/** Make the text block's DOM children equal its inline nodes, touching as few DOM nodes as possible. */
function patchInlines(el: HTMLElement, inlines: Inline[], ctx: RenderContext): void {
  const existing = Array.from(el.childNodes).filter((n) => !isFiller(n));
  inlines.forEach((inline, i) => {
    const cur = existing[i];
    if (inlineMatches(cur, inline)) {
      if (inline.type === "text") {
        const t = innerText(cur!)!;
        if (t.data !== inline.text) t.data = inline.text; // in place: keeps spellcheck state and the IME's node
      }
      return;
    }
    const fresh = createInline(inline, ctx);
    if (cur) el.replaceChild(fresh, cur);
    else {
      const filler = Array.from(el.childNodes).find(isFiller) ?? null;
      el.insertBefore(fresh, filler);
    }
  });
  for (const extra of existing.slice(inlines.length)) el.removeChild(extra);
  const last = inlines[inlines.length - 1];
  const needsFiller = inlines.length === 0 || last!.type === "hard_break";
  const filler = Array.from(el.childNodes).find(isFiller);
  if (needsFiller && !filler) {
    const br = document.createElement("br");
    br.dataset.filler = "";
    el.appendChild(br);
  } else if (!needsFiller && filler) el.removeChild(filler);
}

function containerOf(el: HTMLElement, node: Node): HTMLElement {
  return node.type === "table" ? (el.firstElementChild as HTMLElement) : el;
}

function syncElement(node: Node, el: HTMLElement, ctx: RenderContext, old: Node | undefined): void {
  if (isTextBlock(node)) {
    const style = paragraphStyle(node);
    if ((el.getAttribute("style") ?? "") !== style) {
      if (style) el.setAttribute("style", style);
      else el.removeAttribute("style");
    }
    patchInlines(el, node.children ?? [], ctx);
    return;
  }
  reconcile(containerOf(el, node), node.children as Node[], old ? ((old as { children: Node[] }).children ?? null) : null, ctx);
}

function reconcile(parent: HTMLElement, nodes: Node[], oldNodes: Node[] | null, ctx: RenderContext): void {
  const oldById = new Map((oldNodes ?? []).map((n) => [n.id, n]));
  const elById = new Map<string, HTMLElement>();
  for (const child of Array.from(parent.children)) {
    if (child instanceof HTMLElement && child.dataset.id) elById.set(child.dataset.id, child);
  }
  const keep = new Set<HTMLElement>();
  nodes.forEach((node, i) => {
    let el = elById.get(node.id);
    const old = oldById.get(node.id);
    if (el && el.tagName.toLowerCase() !== tagFor(node)) {
      el = undefined; // heading level / list kind / cell kind changed: a different element
      elById.delete(node.id);
    }
    if (el && old === node) {
      // identical object: nothing to do
    } else {
      const reused = !!el;
      if (!el) el = createElementFor(node);
      syncElement(node, el, ctx, reused ? old : undefined);
    }
    keep.add(el);
    const at = parent.children[i];
    if (at !== el) parent.insertBefore(el, at ?? null);
  });
  for (const child of Array.from(parent.children)) {
    if (!keep.has(child as HTMLElement)) parent.removeChild(child);
  }
}

/** Re-draw ONE text block from the model (the repair path when the DOM drifted from it). */
export function resyncBlock(root: HTMLElement, block: TextBlock, ctx: RenderContext): void {
  const el = findBlockElement(root, block.id);
  if (el) syncElement(block, el, ctx, undefined);
}

/** Bring the root's DOM in line with `doc`. Only blocks whose objects changed since `prev` are touched. */
export function renderDocument(root: HTMLElement, doc: Doc, prev: Doc | null, ctx: RenderContext): void {
  reconcile(root, doc.blocks as Node[], prev ? (prev.blocks as Node[]) : null, ctx);
}

// ---- DOM -> model positions -------------------------------------------------------------------------------------

export function lengthOfDom(node: globalThis.Node): number {
  if (node.nodeType === 3) return (node as Text).data.length;
  if (node instanceof HTMLElement) {
    if (node.dataset.filler !== undefined) return 0;
    if (node.dataset.atom !== undefined) return 1;
  }
  let n = 0;
  for (const c of Array.from(node.childNodes)) n += lengthOfDom(c);
  return n;
}

function textBlockElementOf(root: HTMLElement, node: globalThis.Node): HTMLElement | null {
  for (let cur: globalThis.Node | null = node; cur && cur !== root; cur = cur.parentNode) {
    if (cur instanceof HTMLElement && cur.dataset.kind === "textblock") return cur;
  }
  return null;
}

/** Sum of the lengths of everything inside `el` that comes before the point (target, offset). */
function offsetWithin(el: HTMLElement, target: globalThis.Node, targetOffset: number): number {
  let total = 0;
  const visit = (node: globalThis.Node): boolean => {
    if (node === target) {
      if (node.nodeType === 3) total += targetOffset;
      else for (const c of Array.from(node.childNodes).slice(0, targetOffset)) total += lengthOfDom(c);
      return true;
    }
    if (node.nodeType === 1 && !(node instanceof HTMLElement && (node.dataset.atom !== undefined || node.dataset.filler !== undefined))) {
      for (const c of Array.from(node.childNodes)) if (visit(c)) return true;
      return false;
    }
    total += lengthOfDom(node);
    return false;
  };
  for (const c of Array.from(el.childNodes)) if (visit(c)) break;
  if (target === el) {
    total = 0;
    for (const c of Array.from(el.childNodes).slice(0, targetOffset)) total += lengthOfDom(c);
  }
  return total;
}

function firstTextBlock(el: globalThis.Node): HTMLElement | null {
  if (el instanceof HTMLElement && el.dataset.kind === "textblock") return el;
  for (const c of Array.from(el.childNodes)) {
    const r = firstTextBlock(c);
    if (r) return r;
  }
  return null;
}
function lastTextBlock(el: globalThis.Node): HTMLElement | null {
  if (el instanceof HTMLElement && el.dataset.kind === "textblock") return el;
  for (const c of Array.from(el.childNodes).reverse()) {
    const r = lastTextBlock(c);
    if (r) return r;
  }
  return null;
}

export function pointToPos(root: HTMLElement, node: globalThis.Node, offset: number): Pos | null {
  if (!root.contains(node) && node !== root) return null;
  const tb = textBlockElementOf(root, node);
  if (tb) return { id: tb.dataset.id!, offset: offsetWithin(tb, node, offset) };
  if (node.nodeType === 3) return null;
  const kids = Array.from(node.childNodes);
  for (let i = offset; i < kids.length; i++) {
    const found = firstTextBlock(kids[i]!);
    if (found) return { id: found.dataset.id!, offset: 0 };
  }
  for (let i = Math.min(offset, kids.length) - 1; i >= 0; i--) {
    const found = lastTextBlock(kids[i]!);
    if (found) return { id: found.dataset.id!, offset: lengthOfDom(found) };
  }
  // the point is in a container with no text block inside: look outward
  return node === root ? null : pointToPos(root, node.parentNode!, Array.from(node.parentNode!.childNodes).indexOf(node as ChildNode) + 1);
}

export function findBlockElement(root: HTMLElement, id: string): HTMLElement | null {
  const quoted = id.split("\\").join("\\\\").split('"').join('\\"');
  return root.querySelector<HTMLElement>(`[data-id="${quoted}"]`);
}

/** The DOM point (node, offset) for a model position; text-node ends are preferred so typing continues in place. */
export function posToPoint(root: HTMLElement, pos: Pos): { node: globalThis.Node; offset: number } | null {
  const tb = findBlockElement(root, pos.id);
  if (!tb) return null;
  let remaining = pos.offset;
  const find = (el: HTMLElement): { node: globalThis.Node; offset: number } | null => {
    const kids = Array.from(el.childNodes);
    for (let i = 0; i < kids.length; i++) {
      const child = kids[i]!;
      if (isFiller(child)) continue;
      const len = lengthOfDom(child);
      if (child.nodeType === 3) {
        if (remaining <= len) return { node: child, offset: remaining };
        remaining -= len;
      } else if (isAtom(child)) {
        if (remaining === 0) return { node: el, offset: i };
        remaining -= 1;
      } else if (remaining <= len) {
        const inner = find(child as HTMLElement);
        if (inner) return inner;
      } else remaining -= len;
    }
    return null;
  };
  const hit = find(tb);
  if (hit) return hit;
  const fillers = Array.from(tb.childNodes).filter(isFiller).length;
  return { node: tb, offset: tb.childNodes.length - fillers };
}

// ---- reading a text block back out of the DOM ---------------------------------------------------------------------------

function marksFromAncestors(node: globalThis.Node, stop: HTMLElement): Marks | undefined {
  for (let cur = node.parentNode; cur && cur !== stop; cur = cur.parentNode) {
    if (cur instanceof HTMLElement && cur.dataset.m) {
      try {
        return JSON.parse(cur.dataset.m) as Marks;
      } catch {
        return undefined;
      }
    }
  }
  return undefined;
}

/**
 * Rebuild a text block's inline nodes from its DOM. Used after IME composition (the browser edited the
 * DOM itself) and as the safety net when an input event was not handled by the model first. Images are
 * looked up in the old block by id (the DOM only carries a reference); stray <br> elements the browser
 * adds as placeholders are ignored.
 */
export function readInlines(el: HTMLElement, old: TextBlock): Inline[] {
  const images = new Map((old.children ?? []).flatMap((c) => (c.type === "image" ? [[c.id, c] as const] : [])));
  const out: Inline[] = [];
  const walk = (node: globalThis.Node) => {
    if (node.nodeType === 3) {
      const text = sanitizeText((node as Text).data);
      if (text) {
        const marks = marksFromAncestors(node, el);
        const t: TextNode = marks && Object.keys(marks).length ? { type: "text", text, marks } : { type: "text", text };
        out.push(t);
      }
      return;
    }
    if (!(node instanceof HTMLElement)) return;
    if (node.dataset.filler !== undefined) return;
    if (node.dataset.atom === "hard_break") return void out.push({ type: "hard_break" });
    if (node.dataset.atom === "image") {
      const image = images.get(node.dataset.id ?? "");
      if (image) out.push(image);
      return;
    }
    if (node.tagName === "BR") return;
    for (const c of Array.from(node.childNodes)) walk(c);
  };
  for (const c of Array.from(el.childNodes)) walk(c);
  return normalizeInlines(out);
}

/** The text the DOM shows for a block, atoms as U+FFFC: compared with the model to detect drift. */
export function domText(el: HTMLElement): string {
  let s = "";
  const walk = (node: globalThis.Node) => {
    if (node.nodeType === 3) s += (node as Text).data;
    else if (node instanceof HTMLElement) {
      if (node.dataset.filler !== undefined) return;
      if (node.dataset.atom !== undefined) s += "￼";
      else for (const c of Array.from(node.childNodes)) walk(c);
    }
  };
  for (const c of Array.from(el.childNodes)) walk(c);
  return s;
}
