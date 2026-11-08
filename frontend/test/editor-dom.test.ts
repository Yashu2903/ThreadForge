import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { validateDocument } from "threadforge-client/document-schema";
import { caret } from "../src/editor/commands";
import type { Doc } from "../src/editor/doc";
import { textBlock } from "../src/editor/doc";
import { domText, findBlockElement, lengthOfDom, pointToPos, posToPoint, readInlines, renderDocument } from "../src/editor/dom";
import { Editor, type EditorOptions } from "../src/editor/editor";
import { flatten } from "../src/editor/inline";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const t = (text: string, marks?: object) => ({ type: "text" as const, text, ...(marks ? { marks } : {}) });
const p = (n: number, ...children: object[]) => ({ type: "paragraph" as const, id: id(n), ...(children.length ? { children } : {}) });
const doc = (...blocks: object[]): Doc => ({ schema_version: 1, blocks: blocks as never });
const ctx = { assetUrl: (a: string) => `/assets/${a}` };

let root: HTMLElement;
beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});
afterEach(() => root.remove());

function make(d: Doc, extra: Partial<EditorOptions> = {}) {
  const changes: string[] = [];
  const notices: string[] = [];
  const editor = new Editor(root, { document: d, onChange: (_d, i) => changes.push(i.kind), onNotice: (m) => notices.push(m), ...extra });
  return { editor, changes, notices };
}

/** Dispatch a beforeinput the way a browser would; returns true if the editor cancelled it. */
function input(inputType: string, data?: string | null, init: Partial<InputEventInit> = {}): boolean {
  const e = new InputEvent("beforeinput", { inputType, data: data ?? null, cancelable: true, bubbles: true, ...init });
  root.dispatchEvent(e);
  return e.defaultPrevented;
}
const place = (editor: Editor, n: number, offset: number) => editor.setSelection(caret(id(n), offset));
const textOf = (editor: Editor, n: number) => flatten(textBlock(editor.getDocument(), id(n)).children);

describe("rendering", () => {
  it("gives every node an element with its id, text blocks carry data-kind, and the root is a textbox", () => {
    const { editor } = make(doc({ type: "heading", id: id(1), level: 2, children: [t("Hi")] }, p(2, t("x"))));
    expect(root.getAttribute("role")).toBe("textbox");
    expect(root.getAttribute("aria-multiline")).toBe("true");
    expect(root.getAttribute("aria-label")).toBe("Document");
    expect(root.getAttribute("contenteditable")).toBe("true");
    expect(root.querySelector("h2[data-id][data-kind=textblock]")?.textContent).toBe("Hi");
    expect(findBlockElement(root, id(2))?.tagName).toBe("P");
    editor.destroy();
  });

  it("an empty paragraph gets a zero-length filler; a block ending in a hard break gets one too", () => {
    const { editor } = make(doc(p(1), p(2, t("a"), { type: "hard_break" })));
    for (const n of [1, 2]) {
      const el = findBlockElement(root, id(n))!;
      expect(el.querySelectorAll("br[data-filler]")).toHaveLength(1);
    }
    expect(lengthOfDom(findBlockElement(root, id(1))!)).toBe(0);
    expect(lengthOfDom(findBlockElement(root, id(2))!)).toBe(2);
    editor.destroy();
  });

  it("marks become wrapper elements carrying data-m, with the text node innermost", () => {
    make(doc(p(1, t("plain "), t("bold", { bold: true, color: "#FF0000" }), t(" link", { link: { href: "https://e.com" } }))));
    const el = findBlockElement(root, id(1))!;
    expect(el.childNodes).toHaveLength(3);
    const bold = el.childNodes[1] as HTMLElement;
    expect(JSON.parse(bold.dataset.m!)).toEqual({ bold: true, color: "#FF0000" });
    expect(bold.querySelector("strong")?.textContent).toBe("bold");
    const a = el.querySelector("a")!;
    expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    expect(a.getAttribute("target")).toBe("_blank");
  });

  it("only blocks whose objects changed are touched (identity)", () => {
    const d1 = doc(p(1, t("one")), p(2, t("two")), p(3, t("three")));
    renderDocument(root, d1, null, ctx);
    const els = [1, 2, 3].map((n) => findBlockElement(root, id(n))!);
    const mutations: string[] = [];
    const obs = new MutationObserver((records) => records.forEach((r) => mutations.push((r.target as HTMLElement).dataset?.id ?? r.target.nodeName)));
    obs.observe(root, { subtree: true, childList: true, characterData: true, attributes: true });
    const d2 = { ...d1, blocks: [d1.blocks[0]!, { ...(d1.blocks[1] as object), children: [t("two!")] }, d1.blocks[2]!] } as Doc;
    renderDocument(root, d2, d1, ctx);
    return Promise.resolve().then(() => {
      obs.disconnect();
      expect([1, 2, 3].map((n) => findBlockElement(root, id(n)))).toEqual(els); // same element objects
      expect(domText(els[1]!)).toBe("two!");
      expect(new Set(mutations).size).toBeLessThanOrEqual(2); // the changed text node and nothing else
    });
  });

  it("text is updated in place: the same DOM text node survives a keystroke", () => {
    const { editor } = make(doc(p(1, t("abc"))));
    const textNode = findBlockElement(root, id(1))!.firstChild;
    place(editor, 1, 3);
    input("insertText", "d");
    expect(findBlockElement(root, id(1))!.firstChild).toBe(textNode);
    expect((textNode as Text).data).toBe("abcd");
  });

  it("reorders, removes and recreates elements when blocks come and go", () => {
    const a = doc(p(1, t("a")), p(2, t("b")));
    renderDocument(root, a, null, ctx);
    const b = doc(p(2, t("b")), p(3, t("c")));
    renderDocument(root, b, a, ctx);
    expect(Array.from(root.children).map((c) => (c as HTMLElement).dataset.id)).toEqual([id(2), id(3)]);
  });

  it("images are non-editable atoms with their size and alt text", () => {
    make(doc(p(1, t("x"), { type: "image", id: id(9), asset_id: id(8), width_px: 40, height_px: 30, alt: "A chart" })));
    const img = root.querySelector("img")!;
    expect(img.getAttribute("contenteditable")).toBe("false");
    expect(img.getAttribute("src")).toBe(`/assets/${id(8)}`);
    expect(img.getAttribute("alt")).toBe("A chart");
    expect(lengthOfDom(findBlockElement(root, id(1))!)).toBe(2);
  });
});

describe("selection mapping", () => {
  const rich = doc(p(1, t("ab"), t("cd", { bold: true }), { type: "hard_break" }, t("ef"), { type: "image", id: id(9), asset_id: id(8), width_px: 5, height_px: 5 }, t("gh")));

  it("model -> DOM -> model round-trips every offset, including around atoms and marked runs", () => {
    renderDocument(root, rich, null, ctx);
    const length = 2 + 2 + 1 + 2 + 1 + 2;
    for (let offset = 0; offset <= length; offset++) {
      const point = posToPoint(root, { id: id(1), offset })!;
      expect(point, `offset ${offset}`).not.toBeNull();
      expect(pointToPos(root, point.node, point.offset), `offset ${offset}`).toEqual({ id: id(1), offset });
    }
  });

  it("prefers the end of a text node, so typing continues in the same node", () => {
    renderDocument(root, rich, null, ctx);
    const point = posToPoint(root, { id: id(1), offset: 2 })!;
    expect(point.node.nodeType).toBe(3);
    expect((point.node as Text).data).toBe("ab");
    expect(point.offset).toBe(2);
  });

  it("an empty block maps to (block, 0) and back", () => {
    renderDocument(root, doc(p(1)), null, ctx);
    const point = posToPoint(root, { id: id(1), offset: 0 })!;
    expect(point.node).toBe(findBlockElement(root, id(1)));
    expect(pointToPos(root, point.node, point.offset)).toEqual({ id: id(1), offset: 0 });
  });

  it("a point on an element boundary counts the children before it", () => {
    renderDocument(root, rich, null, ctx);
    const el = findBlockElement(root, id(1))!;
    expect(pointToPos(root, el, 0)).toEqual({ id: id(1), offset: 0 });
    expect(pointToPos(root, el, 2)).toEqual({ id: id(1), offset: 4 }); // after "ab" and the bold run
    expect(pointToPos(root, el, 3)).toEqual({ id: id(1), offset: 5 }); // after the hard break
  });

  it("a point between blocks maps to the nearest text block", () => {
    renderDocument(root, doc(p(1, t("a")), p(2, t("bb"))), null, ctx);
    expect(pointToPos(root, root, 1)).toEqual({ id: id(2), offset: 0 });
    expect(pointToPos(root, root, 2)).toEqual({ id: id(2), offset: 2 });
  });

  it("works inside table cells, lists and quotes", () => {
    const d = doc(
      { type: "table", id: id(10), children: [{ type: "table_row", id: id(11), children: [{ type: "table_cell", id: id(12), children: [p(1, t("cell"))] }] }] },
      { type: "list", id: id(20), children: [{ type: "list_item", id: id(21), children: [p(2, t("item"))] }] },
      { type: "quote", id: id(30), children: [p(3, t("quoted"))] },
    );
    renderDocument(root, d, null, ctx);
    for (const [n, len] of [[1, 4], [2, 4], [3, 6]] as const) {
      const point = posToPoint(root, { id: id(n), offset: len })!;
      expect(pointToPos(root, point.node, point.offset)).toEqual({ id: id(n), offset: len });
    }
    expect(root.querySelector("table td p")?.textContent).toBe("cell");
  });

  it("ignores points outside the editor", () => {
    renderDocument(root, doc(p(1, t("a"))), null, ctx);
    expect(pointToPos(root, document.body, 0)).toBeNull();
  });

  it("readInlines reads text, marks, hard breaks and images back, ignoring fillers and stray <br>", () => {
    renderDocument(root, rich, null, ctx);
    const el = findBlockElement(root, id(1))!;
    el.appendChild(document.createElement("br"));
    expect(readInlines(el, textBlock(rich, id(1)))).toEqual(textBlock(rich, id(1)).children);
  });
});

describe("beforeinput pipeline", () => {
  it("typing: the event is cancelled, the model changes, the caret moves, the DOM follows", () => {
    const { editor, changes } = make(doc(p(1, t("hello"))));
    place(editor, 1, 5);
    expect(input("insertText", " world")).toBe(true);
    expect(textOf(editor, 1)).toBe("hello world");
    expect(domText(findBlockElement(root, id(1))!)).toBe("hello world");
    expect(editor.getSelection()).toEqual(caret(id(1), 11));
    expect(changes).toEqual(["typing"]);
    expect(editor.findDrift()).toEqual([]);
  });

  it("Enter splits (first part keeps the id), Shift+Enter inserts a hard break", () => {
    const { editor } = make(doc(p(1, t("abcd"))));
    place(editor, 1, 2);
    expect(input("insertParagraph")).toBe(true);
    const blocks = editor.getDocument().blocks;
    expect(blocks).toHaveLength(2);
    expect(blocks[0]!.id).toBe(id(1));
    expect(editor.getSelection()!.focus.id).toBe(blocks[1]!.id);
    place(editor, 1, 1);
    input("insertLineBreak");
    expect(textBlock(editor.getDocument(), id(1)).children!.map((c) => c.type)).toEqual(["text", "hard_break", "text"]);
    expect(editor.findDrift()).toEqual([]);
  });

  it("Backspace / Delete, word and line deletion, selection deletion", () => {
    const { editor } = make(doc(p(1, t("one two three"))));
    place(editor, 1, 7);
    input("deleteContentBackward");
    expect(textOf(editor, 1)).toBe("one tw three");
    input("deleteWordBackward");
    expect(textOf(editor, 1)).toBe("one  three");
    place(editor, 1, 3);
    input("deleteContentForward");
    expect(textOf(editor, 1)).toBe("one three");
    editor.setSelection({ anchor: { id: id(1), offset: 1 }, focus: { id: id(1), offset: 4 } });
    input("deleteContent");
    expect(textOf(editor, 1)).toBe("othree");
    input("deleteHardLineBackward");
    expect(textOf(editor, 1)).toBe("three"); // the caret was after "o": everything before it on the line goes
    expect(editor.findDrift()).toEqual([]);
  });

  it("every cancelled type changes nothing: formatting, drops, transposes, unknown types", () => {
    const { editor, changes } = make(doc(p(1, t("keep"))));
    place(editor, 1, 2);
    const before = editor.getDocument();
    for (const type of ["formatBold", "formatItalic", "insertFromDrop", "deleteByDrag", "insertTranspose", "insertOrderedList", "somethingNew", "insertFromYank"]) {
      expect(input(type, "x"), type).toBe(true);
    }
    expect(editor.getDocument()).toBe(before);
    expect(changes).toEqual([]);
  });

  it("insertReplacementText replaces the selection (spellcheck fallback)", () => {
    const { editor } = make(doc(p(1, t("teh cat"))));
    editor.setSelection({ anchor: { id: id(1), offset: 0 }, focus: { id: id(1), offset: 3 } });
    input("insertReplacementText", "the");
    expect(textOf(editor, 1)).toBe("the cat");
  });

  it("historyUndo / historyRedo run the editor's own history, not the browser's", () => {
    const { editor } = make(doc(p(1, t("a"))));
    place(editor, 1, 1);
    input("insertText", "b");
    expect(input("historyUndo")).toBe(true);
    expect(textOf(editor, 1)).toBe("a");
    expect(input("historyRedo")).toBe(true);
    expect(textOf(editor, 1)).toBe("ab");
  });

  it("a read-only editor cancels everything", () => {
    const { editor, changes } = make(doc(p(1, t("a"))), { readOnly: true });
    place(editor, 1, 1);
    expect(root.getAttribute("contenteditable")).toBe("false");
    expect(root.getAttribute("aria-readonly")).toBe("true");
    expect(input("insertText", "x")).toBe(true);
    expect(textOf(editor, 1)).toBe("a");
    expect(changes).toEqual([]);
    editor.setReadOnly(false);
    expect(root.getAttribute("contenteditable")).toBe("true");
  });

  it("dragging text is disabled", () => {
    make(doc(p(1, t("a"))));
    for (const type of ["dragstart", "dragover", "drop"]) {
      const e = new Event(type, { cancelable: true, bubbles: true });
      root.dispatchEvent(e);
      expect(e.defaultPrevented, type).toBe(true);
    }
  });

  it("an input event that was not preceded by a handled beforeinput is read back from the DOM", () => {
    const { editor, changes } = make(doc(p(1, t("abc"))));
    place(editor, 1, 3);
    (findBlockElement(root, id(1))!.firstChild as Text).data = "abcX"; // the browser edited the DOM itself
    root.dispatchEvent(new Event("input", { bubbles: true }));
    expect(textOf(editor, 1)).toBe("abcX");
    expect(editor.stats.domReconciles).toBe(1);
    expect(changes).toEqual(["dom"]);
  });

  it("findDrift reports a block whose DOM no longer matches the model", () => {
    const { editor } = make(doc(p(1, t("abc")), p(2, t("def"))));
    (findBlockElement(root, id(2))!.firstChild as Text).data = "tampered";
    expect(editor.findDrift()).toEqual([id(2)]);
  });
});

describe("IME composition", () => {
  it("makes no model commits and sends no change events until compositionend", async () => {
    vi.useFakeTimers();
    const { editor, changes } = make(doc(p(1, t("ab"))));
    place(editor, 1, 2);
    root.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    expect(editor.isComposing).toBe(true);
    // the browser mutates the DOM during composition; beforeinput must NOT be cancelled
    const during = new InputEvent("beforeinput", { inputType: "insertCompositionText", data: "に", cancelable: true, bubbles: true, isComposing: true });
    root.dispatchEvent(during);
    expect(during.defaultPrevented).toBe(false);
    (findBlockElement(root, id(1))!.firstChild as Text).data = "abに";
    root.dispatchEvent(new InputEvent("input", { inputType: "insertCompositionText", data: "に", bubbles: true, isComposing: true }));
    expect(textOf(editor, 1)).toBe("ab"); // model untouched
    expect(changes).toEqual([]);
    root.dispatchEvent(new CompositionEvent("compositionend", { data: "に", bubbles: true }));
    await vi.runAllTimersAsync();
    expect(editor.isComposing).toBe(false);
    expect(textOf(editor, 1)).toBe("abに");
    expect(changes).toEqual(["composition"]); // exactly one commit
    expect(editor.historySize).toBe(1);
    expect(editor.findDrift()).toEqual([]);
    vi.useRealTimers();
  });

  it("keeps the marks of the run the composition happened in", async () => {
    vi.useFakeTimers();
    const { editor } = make(doc(p(1, t("ab", { bold: true }))));
    place(editor, 1, 2);
    root.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    const textNode = findBlockElement(root, id(1))!.querySelector("strong")!.firstChild as Text;
    textNode.data = "abか";
    root.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    await vi.runAllTimersAsync();
    expect(textBlock(editor.getDocument(), id(1)).children).toEqual([t("abか", { bold: true })]);
    vi.useRealTimers();
  });

  it("an undo after a composition removes the whole composed text", async () => {
    vi.useFakeTimers();
    const { editor } = make(doc(p(1, t("ab"))));
    place(editor, 1, 2);
    root.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    (findBlockElement(root, id(1))!.firstChild as Text).data = "abこんにちは";
    root.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    await vi.runAllTimersAsync();
    editor.undo();
    expect(textOf(editor, 1)).toBe("ab");
    vi.useRealTimers();
  });
});

describe("clipboard", () => {
  function clipboard(type: string, text = "") {
    const e = new Event(type, { cancelable: true, bubbles: true }) as Event & { clipboardData: unknown };
    const data = new Map<string, string>([["text/plain", text]]);
    e.clipboardData = { getData: (k: string) => data.get(k) ?? "", setData: (k: string, v: string) => void data.set(k, v) };
    root.dispatchEvent(e);
    return { e, data };
  }

  it("paste is plain text only: lines become paragraphs, the event is cancelled", () => {
    const { editor } = make(doc(p(1, t("ad"))));
    place(editor, 1, 1);
    const { e } = clipboard("paste", "b\r\nc");
    expect(e.defaultPrevented).toBe(true);
    expect(editor.getDocument().blocks.map((b) => flatten((b as never as { children: never[] }).children))).toEqual(["ab", "cd"]);
  });

  it("a paste that would break a limit is rejected with a message and changes nothing", () => {
    const { editor, notices } = make(doc(p(1, t("abcde"))), {
      limits: { ...(await_defaults()), maxTextCodepoints: 8 },
    });
    place(editor, 1, 5);
    const before = editor.getDocument();
    clipboard("paste", "12345678");
    expect(editor.getDocument()).toBe(before);
    expect(notices[0]).toMatch(/paste was not applied.*8 characters/);
  });

  it("copy puts plain text on the clipboard; cut also deletes", () => {
    const { editor } = make(doc(p(1, t("hello world"))));
    editor.setSelection({ anchor: { id: id(1), offset: 0 }, focus: { id: id(1), offset: 5 } });
    const copied = clipboard("copy");
    expect(copied.data.get("text/plain")).toBe("hello");
    expect(textOf(editor, 1)).toBe("hello world");
    const cut = clipboard("cut");
    expect(cut.data.get("text/plain")).toBe("hello");
    expect(textOf(editor, 1)).toBe(" world");
  });

  it("typing and pasting across table cells shows the hint and changes nothing", () => {
    const table = {
      type: "table",
      id: id(10),
      children: [{ type: "table_row", id: id(11), children: [
        { type: "table_cell", id: id(12), children: [p(1, t("ab"))] },
        { type: "table_cell", id: id(13), children: [p(2, t("cd"))] },
      ] }],
    };
    const { editor, notices } = make(doc(table));
    editor.setSelection({ anchor: { id: id(1), offset: 1 }, focus: { id: id(2), offset: 1 } });
    const before = editor.getDocument();
    input("insertText", "x");
    input("deleteContentBackward");
    clipboard("paste", "zzz");
    expect(editor.getDocument()).toBe(before);
    expect(notices.length).toBe(3);
    expect(notices[0]).toMatch(/spans table cells/);
  });
});

describe("undo and redo", () => {
  function typeChars(editor: Editor, n: number, chars: string) {
    for (const ch of chars) {
      input("insertText", ch);
    }
    void n;
  }

  it("a run of typing in one block is ONE undo step", () => {
    const { editor } = make(doc(p(1, t("a"))));
    place(editor, 1, 1);
    typeChars(editor, 1, "bcdef");
    expect(editor.historySize).toBe(1);
    editor.undo();
    expect(textOf(editor, 1)).toBe("a");
    editor.redo();
    expect(textOf(editor, 1)).toBe("abcdef");
  });

  it("typing pauses of a second or more, caret moves, Enter and deletes make separate steps", () => {
    vi.useFakeTimers();
    const { editor } = make(doc(p(1, t("a"))));
    place(editor, 1, 1);
    input("insertText", "b");
    vi.advanceTimersByTime(1500);
    input("insertText", "c"); // paused: new step
    expect(editor.historySize).toBe(2);
    input("insertParagraph");
    input("insertText", "d");
    expect(editor.historySize).toBe(4);
    input("deleteContentBackward");
    expect(editor.historySize).toBe(5);
    vi.useRealTimers();
  });

  it("restores the selection that existed before the undone step", () => {
    const { editor } = make(doc(p(1, t("hello"))));
    editor.setSelection({ anchor: { id: id(1), offset: 1 }, focus: { id: id(1), offset: 4 } });
    input("deleteContent");
    editor.undo();
    expect(editor.getSelection()).toEqual({ anchor: { id: id(1), offset: 1 }, focus: { id: id(1), offset: 4 } });
  });

  it("Ctrl/Cmd+Z, Shift+Z and Y are handled by the editor", () => {
    const { editor } = make(doc(p(1, t("a"))));
    place(editor, 1, 1);
    input("insertText", "b");
    const key = (init: KeyboardEventInit) => {
      const e = new KeyboardEvent("keydown", { cancelable: true, bubbles: true, ...init });
      root.dispatchEvent(e);
      return e.defaultPrevented;
    };
    expect(key({ key: "z", ctrlKey: true })).toBe(true);
    expect(textOf(editor, 1)).toBe("a");
    expect(key({ key: "z", ctrlKey: true, shiftKey: true })).toBe(true);
    expect(textOf(editor, 1)).toBe("ab");
    key({ key: "z", metaKey: true });
    expect(key({ key: "y", ctrlKey: true })).toBe(true);
    expect(textOf(editor, 1)).toBe("ab");
    expect(key({ key: "z", ctrlKey: true, altKey: true })).toBe(false); // Ctrl+Alt is AltGr on many layouts
  });

  it("a new edit after an undo discards the redo branch", () => {
    const { editor } = make(doc(p(1, t("a"))));
    place(editor, 1, 1);
    input("insertText", "b");
    editor.undo();
    input("insertText", "c");
    expect(editor.canRedo).toBe(false);
    expect(textOf(editor, 1)).toBe("ac");
  });

  it("history is bounded by entry count (oldest first)", () => {
    const { editor } = make(doc(p(1, t("x"))), { history: { maxEntries: 5, groupMs: 0 } });
    place(editor, 1, 1);
    for (let i = 0; i < 12; i++) input("insertText", String(i % 10));
    expect(editor.historySize).toBe(5);
    let undone = 0;
    while (editor.undo()) undone++;
    expect(undone).toBe(5);
    expect(textOf(editor, 1).length).toBe(1 + 7); // 12 typed, the oldest 7 steps can no longer be undone
  });

  it("is bounded by estimated bytes", () => {
    const { editor } = make(doc(p(1, t("x"))), { history: { maxBytes: 600, groupMs: 0 } });
    place(editor, 1, 1);
    for (let i = 0; i < 30; i++) input("insertText", "yyyyyyyy");
    expect(editor.historySize).toBeLessThan(30);
    expect(editor.historySize).toBeGreaterThan(0);
  });

  it("loading a different document starts a fresh history; initial load is not a step", () => {
    const { editor } = make(doc(p(1, t("a"))));
    expect(editor.canUndo).toBe(false);
    place(editor, 1, 1);
    input("insertText", "b");
    expect(editor.canUndo).toBe(true);
    editor.setDocument(doc(p(2, t("other"))));
    expect(editor.canUndo).toBe(false);
    expect(editor.canRedo).toBe(false);
  });

  it("undo never produces a document that fails validation", () => {
    const { editor } = make(doc(p(1, t("a"))), { history: { groupMs: 0 } });
    place(editor, 1, 1);
    for (const step of ["insertText", "insertParagraph", "insertText", "insertLineBreak", "deleteContentBackward"]) {
      input(step, "z");
      expect(() => validateDocument(editor.getDocument())).not.toThrow();
    }
    while (editor.undo()) expect(() => validateDocument(editor.getDocument())).not.toThrow();
    while (editor.redo()) expect(() => validateDocument(editor.getDocument())).not.toThrow();
  });
});

function await_defaults() {
  return {
    maxSerializedBytes: 1024 * 1024,
    maxTextCodepoints: 100_000,
    maxTotalNodes: 60_000,
    maxBlocks: 5_000,
    maxInlineNodes: 20_000,
    maxImages: 100,
    maxListDepth: 3,
    maxLinkLength: 2048,
    allowedFonts: ["sans"],
  };
}
