/** Shared by the pending-edit tests: real Yjs documents, explicit delivery steps, no sockets and no sleeps. */
import * as Y from "yjs";

export const FIELD = "default"; // the editor's fragment name (YJS_FIELD); any name works for the structure under test

export function paragraph(id: string, text: string): Y.XmlElement {
  const element = new Y.XmlElement("paragraph");
  element.setAttribute("id", id);
  const t = new Y.XmlText();
  t.insert(0, text);
  element.insert(0, [t]);
  return element;
}

/** The visible text of every top-level block. */
export function texts(doc: Y.Doc): string[] {
  return doc
    .getXmlFragment(FIELD)
    .toArray()
    .map((n) => (n as Y.XmlElement).toArray().map((c) => (c as Y.XmlText).toString()).join(""));
}

/** Deliver everything `from` has and `to` lacks, the way a sync step does. `origin` is not the doc itself, so the
 *  receiving document sees it as a REMOTE transaction (like the provider's). */
export function deliver(from: Y.Doc, to: Y.Doc, origin: unknown = "provider"): void {
  Y.applyUpdate(to, Y.encodeStateAsUpdate(from, Y.encodeStateVector(to)), origin);
}

/** What the server's AI apply does to a block's text (collab/src/yapply.ts, replace_block_text). */
export function aiReplace(server: Y.Doc, blockIndex: number, newText: string): void {
  server.transact(
    () => {
      const element = server.getXmlFragment(FIELD).get(blockIndex) as Y.XmlElement;
      element.delete(0, element.length);
      const t = new Y.XmlText();
      t.insert(0, newText);
      element.insert(0, [t]);
    },
    { source: "local", context: { source: "ai-apply" } },
  );
}

export function openDocuments(): { server: Y.Doc; offline: Y.Doc; other: Y.Doc } {
  const server = new Y.Doc();
  server.getXmlFragment(FIELD).insert(0, [paragraph("b-1", "Alpha"), paragraph("b-2", "Bravo"), paragraph("b-3", "Charlie")]);
  const offline = new Y.Doc();
  const other = new Y.Doc();
  deliver(server, offline);
  deliver(server, other);
  return { server, offline, other };
}
