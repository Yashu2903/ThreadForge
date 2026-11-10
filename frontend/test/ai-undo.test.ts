/**
 * A person's Ctrl+Z must not undo an AI change, and must still undo the person's own typing.
 *
 * Uses the REAL editor stack: the shared extensions + Tiptap's Collaboration extension (which installs
 * y-prosemirror's UndoManager) bound to a Y.Doc. The AI change is made in a SEPARATE Y.Doc (the server's, with
 * its own origin) and delivered the way the provider delivers remote updates, so it reaches the editor's document
 * with a remote origin that the undo manager does not track.
 */
import { YJS_FIELD, createExtensions } from "@threadforge/editor-schema";
import { Editor } from "@tiptap/core";
import Collaboration from "@tiptap/extension-collaboration";
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

const texts = (editor: Editor) => (editor.getJSON().content ?? []).map((n) => (n.content ?? []).map((c) => (c as { text?: string }).text ?? "").join(""));

// jsdom has no layout: ProseMirror's scroll-into-view asks for client rects
for (const proto of [Range.prototype, Element.prototype]) {
  Object.assign(proto, { getClientRects: () => [], getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }) });
}

function paragraph(id: string, text: string): Y.XmlElement {
  const element = new Y.XmlElement("paragraph");
  element.setAttribute("id", id);
  const t = new Y.XmlText();
  t.insert(0, text);
  element.insert(0, [t]);
  return element;
}

function setup() {
  const server = new Y.Doc();
  const client = new Y.Doc();
  // the document already holds "Alpha" BEFORE the editor opens it, delivered the way the provider delivers state
  server.getXmlFragment(YJS_FIELD).insert(0, [paragraph("00000000-0000-4000-8000-000000000001", "Alpha")]);
  const sync = (from: Y.Doc, to: Y.Doc, origin: unknown) => Y.applyUpdate(to, Y.encodeStateAsUpdate(from, Y.encodeStateVector(to)), origin);
  sync(server, client, "provider");
  const element = document.createElement("div");
  document.body.appendChild(element);
  const editor = new Editor({
    element,
    extensions: [...createExtensions(), Collaboration.configure({ document: client, field: YJS_FIELD })],
  });
  return { server, client, editor, fromServer: () => sync(server, client, "provider") };
}

describe("undo and AI changes", () => {
  it("Ctrl+Z undoes the person's typing but not an AI change that arrived after it", () => {
    const { server, editor, fromServer } = setup();
    editor.commands.focus("end");
    editor.commands.insertContent(" typed by me");
    expect(texts(editor)).toEqual(["Alpha typed by me"]);
    // the AI change: made in the server's document with the AI's own origin, then delivered as a remote update
    server.transact(() => {
      const fragment = server.getXmlFragment(YJS_FIELD);
      fragment.insert(fragment.length, [paragraph("00000000-0000-4000-8000-0000000000aa", "From the AI")]);
    }, { source: "local", context: { source: "ai-apply" } });
    fromServer();
    expect(texts(editor)).toContain("From the AI");
    editor.commands.undo();
    expect(texts(editor)).toContain("From the AI"); // untouched by the person's undo
    expect(texts(editor)[0]).toBe("Alpha"); // the person's own typing was undone
    editor.commands.redo();
    expect(texts(editor)[0]).toBe("Alpha typed by me");
    editor.destroy();
  });

  it("a second Ctrl+Z with nothing of the person's left changes nothing: the AI change cannot be undone", () => {
    const { server, editor, fromServer } = setup();
    server.transact(() => {
      const fragment = server.getXmlFragment(YJS_FIELD);
      fragment.insert(fragment.length, [paragraph("00000000-0000-4000-8000-0000000000bb", "AI only")]);
    }, { source: "local", context: { source: "ai-apply" } });
    fromServer();
    const before = texts(editor);
    editor.commands.undo();
    editor.commands.undo();
    expect(texts(editor)).toEqual(before);
    expect(before).toContain("AI only");
    editor.destroy();
  });
});
