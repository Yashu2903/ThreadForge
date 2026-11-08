/**
 * The editor: a framework-free island. React (or anything else) hands it an element and talks to it
 * through this class's methods; nothing outside renders into the editable root.
 *
 * INPUT STRATEGY (model first). For every input type we support, `beforeinput` is cancelled with
 * preventDefault(), the change is computed on the immutable model (commands.ts), and only the DOM of
 * the blocks that changed is updated (dom.ts). The browser never edits the DOM itself, so the DOM
 * cannot drift from the model. The exceptions are
 *   - IME composition (compositionstart .. compositionend): the browser must own the DOM or the IME
 *     breaks; no model commits and no change events happen until compositionend, when the touched
 *     block is read back from the DOM and committed as ONE history entry;
 *   - input types we do not understand are cancelled, never allowed to edit the DOM silently.
 * After every command the blocks at the caret are compared with the model; a mismatch re-draws the
 * block from the model and logs a warning in development. execCommand is not used anywhere.
 */
import type { Limits, Marks } from "threadforge-client/document-schema";
import { DEFAULT_LIMITS } from "threadforge-client/document-schema";
import {
  type Outcome,
  type Pos,
  type Sel,
  caret,
  deleteForward,
  deleteRange,
  deleteSelection,
  insertHardBreak,
  insertPlainText,
  insertText,
  isCollapsed,
  ordered,
  selectionText,
} from "./commands";
import {
  type AttrKey,
  type AttrPatch,
  type BlockKind,
  type BoolMark,
  type LinkInfo,
  type Tri,
  type ValueMark,
  attrState,
  blockKindState,
  boolMarkState,
  changeIndent,
  linkAt,
  markValueProblem,
  removeLink,
  setAttrs,
  setBlockKind,
  setLink,
  setMark,
  toggleMark,
  valueMarkState,
} from "./format";
import {
  addColumn,
  addRow,
  backspace,
  deleteTable,
  enter,
  indentListItems,
  inQuote,
  insertTable,
  listState,
  outdentListItems,
  removeColumn,
  removeRow,
  tableContext,
  tableTab,
  toggleList,
  toggleQuote,
} from "./structure";
import { type Doc, blockLength, indexOf, isTextBlock, nodeById, textBlock, updateNode, withInlines } from "./doc";
import {
  type RenderContext,
  domText,
  findBlockElement,
  pointToPos,
  posToPoint,
  readInlines,
  renderDocument,
  resyncBlock,
} from "./dom";
import { History, type HistoryOptions } from "./history";
import { flatten, inheritMarks } from "./inline";
import { limitProblem } from "./stats";

export interface EditorOptions {
  document: Doc;
  readOnly?: boolean;
  limits?: Limits;
  assetUrl?: (assetId: string) => string;
  label?: string;
  history?: HistoryOptions;
  /** The document changed because of an edit, undo or redo (never during composition). */
  onChange?: (doc: Doc, info: { kind: string }) => void;
  /** A message for the user: a refused edit, a rejected paste, a blocked selection. */
  onNotice?: (message: string) => void;
  onSelectionChange?: (sel: Sel | null) => void;
  /** Ctrl/Cmd+S. */
  onSave?: () => void;
  /** Ctrl/Cmd+K: the host should open its link editor. */
  onLinkRequest?: () => void;
  /** The selection, the document or the history changed: re-read formatState() (toolbars). */
  onStateChange?: () => void;
  /** Fonts the host allows (document font keys); default = the schema's. */
  fonts?: readonly string[];
}

/** What the toolbar shows for the current selection. Tri-state values are "mixed" over a mixed selection. */
export interface FormatState {
  bold: Tri;
  italic: Tri;
  underline: Tri;
  strike: Tri;
  script: unknown;
  font: unknown;
  size: unknown;
  color: unknown;
  highlight: unknown;
  link: LinkInfo | null;
  block: BlockKind | "mixed";
  quote: boolean;
  align: unknown;
  lineSpacing: unknown;
  spaceBefore: unknown;
  spaceAfter: unknown;
  indent: unknown;
  list: { ordered: boolean | "mixed"; depth: number } | null;
  table: { rows: number; cols: number; rowIndex: number; colIndex: number } | null;
  /** the selection crosses table cells: marks and alignment work, typing and deleting do not */
  multiCell: boolean;
  canUndo: boolean;
  canRedo: boolean;
  readOnly: boolean;
  collapsed: boolean;
}

const DEV = typeof import.meta !== "undefined" && (import.meta as { env?: { DEV?: boolean } }).env?.DEV === true;

export class Editor {
  readonly root: HTMLElement;
  private doc: Doc;
  private history: History;
  private readonly opts: EditorOptions;
  private readonly ctx: RenderContext;
  private readonly limits: Limits;
  private composing = false;
  private compositionBlocks = new Set<string>();
  private readOnly: boolean;
  private lastSet: Sel | null = null;
  /** the last selection that was inside the editor (toolbar controls may take focus away) */
  private lastSel: Sel | null = null;
  /** the selection the last committed command left, applied after toolbar interactions */
  private afterSel: Sel | null = null;
  /** marks to apply to the next text typed at a collapsed caret (not stored in the document) */
  private typing: { id: string; offset: number; marks: Marks } | null = null;
  private destroyed = false;
  private cleanup: (() => void)[] = [];
  /** development counters, asserted by tests */
  readonly stats = { drift: 0, domReconciles: 0 };

  constructor(root: HTMLElement, options: EditorOptions) {
    this.root = root;
    this.opts = options;
    this.doc = options.document;
    this.readOnly = options.readOnly ?? false;
    this.limits = options.limits ?? DEFAULT_LIMITS;
    this.ctx = { assetUrl: options.assetUrl ?? ((id) => `/assets/${id}`) };
    const first = indexOf(this.doc).order[0]!;
    this.history = new History(this.doc, caret(first, 0), options.history);

    root.setAttribute("role", "textbox");
    root.setAttribute("aria-multiline", "true");
    root.setAttribute("aria-label", options.label ?? "Document");
    root.setAttribute("spellcheck", "true");
    root.classList.add("tf-editor");
    this.applyReadOnly();
    renderDocument(root, this.doc, null, this.ctx);

    const on = <K extends keyof HTMLElementEventMap>(type: K, fn: (e: HTMLElementEventMap[K]) => void) => {
      root.addEventListener(type, fn as EventListener);
      this.cleanup.push(() => root.removeEventListener(type, fn as EventListener));
    };
    on("beforeinput", (e) => this.onBeforeInput(e as InputEvent));
    on("input", (e) => this.onInput(e as InputEvent));
    on("keydown", (e) => this.onKeyDown(e));
    on("compositionstart", () => this.onCompositionStart());
    on("compositionend", () => this.onCompositionEnd());
    on("paste", (e) => this.onPaste(e as ClipboardEvent));
    on("copy", (e) => this.onCopy(e as ClipboardEvent, false));
    on("cut", (e) => this.onCopy(e as ClipboardEvent, true));
    on("click", (e) => this.onClick(e as MouseEvent));
    on("dragstart", (e) => e.preventDefault());
    on("dragover", (e) => e.preventDefault());
    on("drop", (e) => e.preventDefault());
    const onSelectionChange = () => this.onSelectionChange();
    document.addEventListener("selectionchange", onSelectionChange);
    this.cleanup.push(() => document.removeEventListener("selectionchange", onSelectionChange));
  }

  // ---- public interface -------------------------------------------------------------------------------------

  getDocument(): Doc {
    return this.doc;
  }
  get isComposing(): boolean {
    return this.composing;
  }
  get canUndo(): boolean {
    return this.history.canUndo;
  }
  get canRedo(): boolean {
    return this.history.canRedo;
  }
  get historySize(): number {
    return this.history.size;
  }

  focus(): void {
    this.root.focus();
  }

  setReadOnly(value: boolean): void {
    this.readOnly = value;
    this.applyReadOnly();
  }

  /**
   * Replace the whole document: loading another one, or adopting a newer server version of a clean
   * one. History starts over (the old snapshots describe a document that no longer exists).
   */
  setDocument(doc: Doc): void {
    this.doc = doc;
    renderDocument(this.root, doc, null, this.ctx);
    this.history.reset(doc, caret(indexOf(doc).order[0]!, 0));
  }

  getSelection(): Sel | null {
    return this.currentSelection();
  }

  setSelection(sel: Sel): void {
    this.applySelection(sel);
  }

  undo(): boolean {
    const step = this.history.undo();
    if (!step) return false;
    this.show(step.doc, step.sel);
    this.opts.onChange?.(this.doc, { kind: "undo" });
    this.opts.onStateChange?.();
    return true;
  }

  redo(): boolean {
    const step = this.history.redo();
    if (!step) return false;
    this.show(step.doc, step.sel);
    this.opts.onChange?.(this.doc, { kind: "redo" });
    this.opts.onStateChange?.();
    return true;
  }

  destroy(): void {
    this.destroyed = true;
    for (const fn of this.cleanup) fn();
    this.cleanup = [];
  }

  /** Development check: every text block's DOM equals the model. Returns the ids that differ. */
  findDrift(): string[] {
    const bad: string[] = [];
    for (const id of indexOf(this.doc).order) {
      const el = findBlockElement(this.root, id);
      if (!el || domText(el) !== flatten(textBlock(this.doc, id).children)) bad.push(id);
    }
    return bad;
  }

  // ---- commands for toolbars and shortcuts ----------------------------------------------------------------------

  private selectionForCommand(): Sel | null {
    return this.currentSelection() ?? this.lastSel ?? caret(indexOf(this.doc).order[0]!, 0);
  }

  /** Run a pure command on the current (or last known) selection, then give focus back to the text. */
  private run(kind: string, fn: (doc: Doc, sel: Sel) => Outcome): boolean {
    if (this.readOnly || this.composing) return false;
    const sel = this.selectionForCommand();
    if (!sel) return false;
    const done = this.commit(kind, fn(this.doc, sel), null, sel);
    if (done) {
      // hand focus (and the selection the command left) back to the text. A refused command does NOT
      // steal focus: the control that asked (a link form, say) stays usable.
      this.root.focus({ preventScroll: true });
      const target = this.afterSel ?? sel;
      this.applySelection(target);
      this.lastSel = target;
    }
    return done;
  }

  toggleMark(key: BoolMark): void {
    const sel = this.selectionForCommand();
    if (!sel || this.readOnly) return;
    if (isCollapsed(sel)) {
      // nothing selected: remember the choice for the next typed character (not stored in the document)
      const base = this.typing?.marks ?? inheritMarks(textBlock(this.doc, sel.focus.id).children, sel.focus.offset) ?? {};
      const marks: Marks = { ...base };
      if (marks[key]) delete marks[key];
      else marks[key] = true;
      this.typing = { id: sel.focus.id, offset: sel.focus.offset, marks };
      this.opts.onStateChange?.();
      this.root.focus({ preventScroll: true });
      return;
    }
    this.run("format", (d, s) => toggleMark(d, s, key));
  }

  setMarkValue(key: ValueMark, value: string | number | null): void {
    const sel = this.selectionForCommand();
    if (!sel || this.readOnly) return;
    const problem = markValueProblem(key, value, this.opts.fonts);
    if (problem) return void this.opts.onNotice?.(problem);
    if (isCollapsed(sel)) {
      const base = this.typing?.marks ?? inheritMarks(textBlock(this.doc, sel.focus.id).children, sel.focus.offset) ?? {};
      const marks: Record<string, unknown> = { ...base };
      if (value === null) delete marks[key];
      else marks[key] = key === "color" || key === "highlight" ? String(value).toUpperCase() : value;
      this.typing = { id: sel.focus.id, offset: sel.focus.offset, marks: marks as Marks };
      this.opts.onStateChange?.();
      this.root.focus({ preventScroll: true });
      return;
    }
    this.run("format", (d, s) => setMark(d, s, key, value));
  }

  setLink(href: string): boolean {
    return this.run("link", (d, s) => setLink(d, s, href));
  }
  removeLink(): boolean {
    return this.run("link", (d, s) => removeLink(d, s));
  }
  setAttrs(patch: AttrPatch): boolean {
    return this.run("format", (d, s) => setAttrs(d, s, patch));
  }
  changeIndent(delta: number): boolean {
    return this.run("format", (d, s) => changeIndent(d, s, delta));
  }
  setBlockKind(kind: BlockKind): boolean {
    return this.run("format", (d, s) => setBlockKind(d, s, kind));
  }
  toggleList(ordered: boolean): boolean {
    return this.run("list", (d, s) => toggleList(d, s, ordered));
  }
  toggleQuote(): boolean {
    return this.run("quote", (d, s) => toggleQuote(d, s));
  }
  /** Indent: list items go one level deeper; anything else gains a paragraph indent level. */
  indent(): boolean {
    const sel = this.selectionForCommand();
    if (sel && listState(this.doc, sel)) return this.run("list", (d, s) => indentListItems(d, s));
    return this.changeIndent(1);
  }
  outdent(): boolean {
    const sel = this.selectionForCommand();
    if (sel && listState(this.doc, sel)) return this.run("list", (d, s) => outdentListItems(d, s));
    return this.changeIndent(-1);
  }
  insertTable(rows: number, cols: number, headerRow: boolean): boolean {
    return this.run("table", (d, s) => insertTable(d, s, rows, cols, headerRow));
  }
  addRow(where: "above" | "below"): boolean {
    return this.run("table", (d, s) => addRow(d, s, where));
  }
  addColumn(where: "left" | "right"): boolean {
    return this.run("table", (d, s) => addColumn(d, s, where));
  }
  removeRow(): boolean {
    return this.run("table", (d, s) => removeRow(d, s));
  }
  removeColumn(): boolean {
    return this.run("table", (d, s) => removeColumn(d, s));
  }
  deleteTable(): boolean {
    return this.run("table", (d, s) => deleteTable(d, s));
  }

  /** Everything a toolbar needs to draw itself for the current selection. */
  formatState(): FormatState {
    const sel = this.selectionForCommand()!;
    const typing = this.typing && isCollapsed(sel) && this.typing.id === sel.focus.id && this.typing.offset === sel.focus.offset ? this.typing.marks : null;
    const d = this.doc;
    const table = tableContext(d, sel.focus.id);
    const cells = new Set(
      ordered(d, sel) && [sel.anchor.id, sel.focus.id].map((id) => tableContext(d, id)?.cell.id ?? null),
    );
    return {
      bold: boolMarkState(d, sel, "bold", typing),
      italic: boolMarkState(d, sel, "italic", typing),
      underline: boolMarkState(d, sel, "underline", typing),
      strike: boolMarkState(d, sel, "strike", typing),
      script: valueMarkState(d, sel, "script", typing),
      font: valueMarkState(d, sel, "font", typing),
      size: valueMarkState(d, sel, "size_pt", typing),
      color: valueMarkState(d, sel, "color", typing),
      highlight: valueMarkState(d, sel, "highlight", typing),
      link: linkAt(d, sel),
      block: blockKindState(d, sel),
      quote: inQuote(d, sel),
      align: attrState(d, sel, "align" as AttrKey),
      lineSpacing: attrState(d, sel, "line_spacing"),
      spaceBefore: attrState(d, sel, "space_before_pt"),
      spaceAfter: attrState(d, sel, "space_after_pt"),
      indent: attrState(d, sel, "indent_level"),
      list: listState(d, sel),
      table: table ? { rows: table.rows, cols: table.cols, rowIndex: table.rowIndex, colIndex: table.colIndex } : null,
      multiCell: cells.size > 1,
      canUndo: this.history.canUndo,
      canRedo: this.history.canRedo,
      readOnly: this.readOnly,
      collapsed: isCollapsed(sel),
    };
  }

  private onClick(e: MouseEvent): void {
    if (!(e.ctrlKey || e.metaKey)) return;
    const a = (e.target as HTMLElement | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
    if (!a || !this.root.contains(a)) return;
    e.preventDefault(); // a link opens only on Ctrl/Cmd-click (or from the link popover), never on a plain click
    window.open(a.getAttribute("href") ?? "", "_blank", "noopener,noreferrer");
  }

  // ---- applying results ----------------------------------------------------------------------------------------

  private applyReadOnly(): void {
    this.root.setAttribute("contenteditable", this.readOnly ? "false" : "true");
    this.root.setAttribute("aria-readonly", String(this.readOnly));
  }

  private show(doc: Doc, sel: Sel): void {
    this.afterSel = sel;
    const prev = this.doc;
    this.doc = doc;
    renderDocument(this.root, doc, prev, this.ctx);
    this.applySelection(sel);
    this.verify(sel);
  }

  /** Run a command result: refuse blocked/over-limit edits, else render, select, record, notify. */
  private commit(kind: string, outcome: Outcome, key: string | null = null, selBefore?: Sel): boolean {
    if (!outcome) return false;
    if ("blocked" in outcome) {
      this.opts.onNotice?.(outcome.blocked);
      return false;
    }
    const problem = limitProblem(this.doc, outcome.doc, this.limits);
    if (problem) {
      this.opts.onNotice?.(problem);
      return false;
    }
    const before = selBefore ?? this.currentSelection() ?? outcome.sel;
    this.show(outcome.doc, outcome.sel);
    this.history.record(outcome.doc, before, outcome.sel, kind, key);
    this.opts.onChange?.(this.doc, { kind });
    this.opts.onStateChange?.();
    return true;
  }

  private verify(sel: Sel): void {
    for (const id of new Set([sel.anchor.id, sel.focus.id])) {
      const node = nodeById(this.doc, id);
      const el = findBlockElement(this.root, id);
      if (!node || !isTextBlock(node) || !el) continue;
      if (domText(el) !== flatten(node.children)) {
        this.stats.drift++;
        if (DEV) console.warn(`[editor] DOM drifted from the model in block ${id}; re-drawing it from the model`);
        resyncBlock(this.root, node, this.ctx);
        this.applySelection(sel);
      }
    }
  }

  // ---- selection ---------------------------------------------------------------------------------------------------

  private currentSelection(): Sel | null {
    const s = this.root.ownerDocument.getSelection();
    if (!s || s.rangeCount === 0 || !s.anchorNode || !s.focusNode) return null;
    const anchor = pointToPos(this.root, s.anchorNode, s.anchorOffset);
    const focus = pointToPos(this.root, s.focusNode, s.focusOffset);
    return anchor && focus ? { anchor, focus } : null;
  }

  private applySelection(sel: Sel): void {
    const a = posToPoint(this.root, sel.anchor);
    const f = posToPoint(this.root, sel.focus);
    const s = this.root.ownerDocument.getSelection();
    if (!a || !f || !s) return;
    this.lastSet = sel;
    s.setBaseAndExtent(a.node, a.offset, f.node, f.offset);
  }

  private onSelectionChange(): void {
    if (this.destroyed) return;
    const sel = this.currentSelection();
    const mine = this.lastSet;
    this.lastSet = null;
    if (!sel) return;
    const same =
      mine &&
      mine.anchor.id === sel.anchor.id &&
      mine.anchor.offset === sel.anchor.offset &&
      mine.focus.id === sel.focus.id &&
      mine.focus.offset === sel.focus.offset;
    if (!same) this.history.seal(); // the user moved the caret: the next keystroke starts a new undo group
    this.lastSel = sel;
    if (this.typing && !(isCollapsed(sel) && sel.focus.id === this.typing.id && sel.focus.offset === this.typing.offset)) this.typing = null;
    this.opts.onSelectionChange?.(sel);
    this.opts.onStateChange?.();
  }

  // ---- input ----------------------------------------------------------------------------------------------------------

  private onBeforeInput(e: InputEvent): void {
    if (this.readOnly) return void e.preventDefault();
    // IME composition owns the DOM until compositionend
    if (e.isComposing || this.composing || e.inputType === "insertCompositionText") return;
    const sel = this.currentSelection();
    const type = e.inputType;

    // never let the browser edit the DOM for anything below
    e.preventDefault();
    if (!sel) return;

    const key = isCollapsed(sel) ? sel.focus.id : null;
    const text = () => e.data ?? e.dataTransfer?.getData("text/plain") ?? "";

    switch (type) {
      case "insertText": {
        const pending = this.typing && isCollapsed(sel) && this.typing.id === sel.focus.id && this.typing.offset === sel.focus.offset ? this.typing.marks : undefined;
        this.typing = null;
        this.commit("typing", insertText(this.doc, sel, text(), pending), key);
        return;
      }
      case "insertReplacementText": {
        // spellcheck / autocorrect: the browser tells us which range it wants replaced
        const target = this.targetRange(e);
        const range = target ?? sel;
        this.commit("replace", insertText(this.doc, range, text()));
        return;
      }
      case "insertLineBreak":
        this.commit("break", insertHardBreak(this.doc, sel));
        return;
      case "insertParagraph":
        this.commit("split", enter(this.doc, sel));
        return;
      case "deleteContentBackward":
        this.commit("delete", backspace(this.doc, sel));
        return;
      case "deleteContentForward":
        this.commit("delete", deleteForward(this.doc, sel));
        return;
      case "deleteWordBackward":
      case "deleteWordForward":
      case "deleteSoftLineBackward":
      case "deleteSoftLineForward":
      case "deleteHardLineBackward":
      case "deleteHardLineForward": {
        const backward = type.endsWith("Backward");
        const unit = type.includes("Word") ? "word" : "line";
        const target = isCollapsed(sel) ? this.targetRange(e) : null;
        if (target && !isCollapsed(target)) this.commit("delete", fromRange(deleteSelection(this.doc, target)));
        else this.commit("delete", backward ? backspace(this.doc, sel, unit) : deleteForward(this.doc, sel, unit));
        return;
      }
      case "deleteContent":
      case "deleteByCut":
        if (!isCollapsed(sel)) this.commit("delete", fromRange(deleteSelection(this.doc, sel)));
        return;
      case "insertFromPaste":
      case "insertFromPasteAsQuotation": {
        const pasted = e.dataTransfer?.getData("text/plain") ?? "";
        if (pasted) this.paste(pasted, sel);
        return;
      }
      case "formatBold":
        return void this.toggleMark("bold");
      case "formatItalic":
        return void this.toggleMark("italic");
      case "formatUnderline":
        return void this.toggleMark("underline");
      case "formatStrikeThrough":
        return void this.toggleMark("strike");
      case "formatJustifyLeft":
        return void this.setAttrs({ align: "left" });
      case "formatJustifyCenter":
        return void this.setAttrs({ align: "center" });
      case "formatJustifyRight":
        return void this.setAttrs({ align: "right" });
      case "formatJustifyFull":
        return void this.setAttrs({ align: "justify" });
      case "insertOrderedList":
        return void this.toggleList(true);
      case "insertUnorderedList":
        return void this.toggleList(false);
      case "formatIndent":
        return void this.indent();
      case "formatOutdent":
        return void this.outdent();
      case "historyUndo":
        this.undo();
        return;
      case "historyRedo":
        this.redo();
        return;
      default:
        // formatBold/…, insertFromDrop, deleteByDrag, insertTranspose, list commands, …: cancelled.
        return;
    }
  }

  /** `input` only reaches us when a beforeinput was NOT cancelled: reconcile that block from the DOM. */
  private onInput(_e: InputEvent): void {
    if (this.composing || this.readOnly) return;
    const sel = this.currentSelection();
    if (!sel) return;
    this.stats.domReconciles++;
    if (DEV) console.warn("[editor] an input event was not preceded by a handled beforeinput; reading the block back from the DOM");
    this.reconcileFromDom([sel.focus.id], "dom");
  }

  private targetRange(e: InputEvent): Sel | null {
    const ranges = typeof e.getTargetRanges === "function" ? e.getTargetRanges() : [];
    const r = ranges[0];
    if (!r) return null;
    const anchor = pointToPos(this.root, r.startContainer, r.startOffset);
    const focus = pointToPos(this.root, r.endContainer, r.endOffset);
    return anchor && focus ? { anchor, focus } : null;
  }

  // ---- IME composition ---------------------------------------------------------------------------------------

  private onCompositionStart(): void {
    if (this.readOnly) return;
    this.composing = true;
    this.history.seal();
    const sel = this.currentSelection();
    this.compositionBlocks = new Set(sel ? [sel.anchor.id, sel.focus.id] : []);
  }

  private onCompositionEnd(): void {
    if (!this.composing) return;
    // The final DOM mutation can land just after compositionend (Chromium) or just before (Safari).
    setTimeout(() => {
      if (this.destroyed) return;
      this.composing = false;
      const sel = this.currentSelection();
      const ids = new Set(this.compositionBlocks);
      if (sel) ids.add(sel.focus.id);
      this.reconcileFromDom([...ids], "composition");
    }, 0);
  }

  /** Read the given blocks back from the DOM into the model as ONE undoable step. */
  private reconcileFromDom(ids: string[], kind: string): void {
    const sel = this.currentSelection();
    let next = this.doc;
    for (const id of ids) {
      const node = nodeById(next, id);
      const el = findBlockElement(this.root, id);
      if (!node || !isTextBlock(node) || !el) continue;
      const inlines = readInlines(el, node);
      if (flatten(inlines) === flatten(node.children) && JSON.stringify(inlines) === JSON.stringify(node.children ?? [])) continue;
      next = updateNode(next, id, (n) => withInlines(n as typeof node, inlines));
    }
    if (next === this.doc) {
      // nothing changed in the model, but the browser may have left the DOM untidy
      renderDocument(this.root, this.doc, null, this.ctx);
      if (sel) this.applySelection(sel);
      return;
    }
    const problem = limitProblem(this.doc, next, this.limits);
    const fallback = sel ?? caret(ids[0]!, 0);
    if (problem) {
      this.opts.onNotice?.(problem);
      renderDocument(this.root, this.doc, null, this.ctx); // put the DOM back the way the model has it
      this.applySelection(fallback);
      return;
    }
    const before = fallback;
    this.doc = next;
    renderDocument(this.root, next, null, this.ctx);
    this.applySelection(fallback);
    this.history.record(next, before, fallback, kind);
    this.opts.onChange?.(this.doc, { kind });
  }

  // ---- keyboard shortcuts --------------------------------------------------------------------------------------

  private onKeyDown(e: KeyboardEvent): void {
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey && !this.readOnly && !this.composing) {
      // Tab moves between table cells and indents list items; anywhere else it leaves the editor
      // (no keyboard trap): the browser's default focus movement is only cancelled when we act.
      const sel = this.selectionForCommand();
      if (sel && tableContext(this.doc, sel.focus.id)) {
        e.preventDefault();
        this.run("tab", (d, s) => tableTab(d, s, e.shiftKey));
      } else if (sel && listState(this.doc, sel)) {
        e.preventDefault();
        if (e.shiftKey) this.outdent();
        else this.indent();
      }
      return;
    }
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return;
    const key = e.key.toLowerCase();
    if (!this.readOnly && !e.shiftKey && (key === "b" || key === "i" || key === "u")) {
      e.preventDefault();
      this.toggleMark(key === "b" ? "bold" : key === "i" ? "italic" : "underline");
    } else if (key === "k" && !e.shiftKey) {
      e.preventDefault();
      if (!this.readOnly) this.opts.onLinkRequest?.();
    } else if (key === "z" && !this.readOnly) {
      e.preventDefault();
      if (e.shiftKey) this.redo();
      else this.undo();
    } else if (key === "y" && !this.readOnly) {
      e.preventDefault();
      this.redo();
    } else if (key === "s") {
      e.preventDefault();
      this.opts.onSave?.();
    }
  }

  // ---- clipboard (plain text only) ---------------------------------------------------------------------------

  private paste(text: string, sel: Sel): void {
    const result = insertPlainText(this.doc, sel, text);
    if (result && !("blocked" in result)) {
      const problem = limitProblem(this.doc, result.doc, this.limits);
      if (problem) return void this.opts.onNotice?.(`This paste was not applied. ${problem}`);
    }
    this.commit("paste", result);
  }

  private onPaste(e: ClipboardEvent): void {
    e.preventDefault(); // never import HTML or images
    if (this.readOnly) return;
    const sel = this.currentSelection();
    const text = e.clipboardData?.getData("text/plain") ?? "";
    if (!sel || !text) return;
    this.paste(text, sel);
  }

  private onCopy(e: ClipboardEvent, cut: boolean): void {
    const sel = this.currentSelection();
    if (!sel || isCollapsed(sel)) return;
    e.preventDefault();
    e.clipboardData?.setData("text/plain", selectionText(this.doc, sel));
    if (cut && !this.readOnly) this.commit("delete", fromRange(deleteSelection(this.doc, sel)));
  }
}

function fromRange(r: { doc: Doc; pos: Pos } | { blocked: string }): Outcome {
  return "blocked" in r ? r : { doc: r.doc, sel: caret(r.pos.id, r.pos.offset) };
}

export { blockLength, deleteRange, ordered };
