/**
 * ROOT CAUSE of the documented lost edit (docs/ai.md 19.6), reproduced deterministically with two real Yjs
 * documents and explicit delivery steps (no sleeps, no sockets).
 *
 * The server replaces a block's text exactly the way collab/src/yapply.ts does it: `element.delete(0, length)`
 * followed by `element.insert(0, [newText])`. That deletes the block's old Y.XmlText NODE. A browser that was
 * offline typed INSIDE that node. When its change merges, Yjs integrates the new characters under a parent that is
 * already deleted, so they are deleted on arrival, and the characters are garbage collected. Nothing in the
 * document, on any side, still holds the words.
 *
 * This file keeps the reproduction so a future reader can see WHY the browser has to keep a recovery copy:
 * the recovery tests are in pending-edit-recovery.test.ts.
 */
import { describe, expect, it } from "vitest";
import * as Y from "yjs";
import { FIELD, aiReplace, deliver, openDocuments, texts } from "./yjs-fixtures";

describe("root cause: an unsent edit inside an AI-replaced block", () => {
  it("is deleted on arrival and no document holds the words afterwards", () => {
    const { server, offline, other } = openDocuments();
    // 1. the browser goes offline and types at the end of "Bravo" (its copy only; nothing was sent)
    const text = (offline.getXmlFragment(FIELD).get(1) as Y.XmlElement).get(0) as Y.XmlText;
    text.insert(text.length, " [offline note]");
    expect(texts(offline)[1]).toBe("Bravo [offline note]");
    // 2. the AI proposal is applied on the server, which cannot see that edit (the base hash still matches)
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, other);
    // 3. it reconnects: the provider delivers the server's changes first, then sends its own
    deliver(server, offline);
    deliver(offline, server, "connection");
    deliver(server, other);
    // 4. everyone converges on the AI's text, and the words are nowhere
    expect(texts(server)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    expect(texts(offline)).toEqual(texts(server));
    expect(texts(other)).toEqual(texts(server));
    for (const doc of [server, offline, other]) expect(JSON.stringify(doc.getXmlFragment(FIELD).toJSON())).not.toContain("offline note");
  });

  it("does not happen to an edit in a block the AI did not touch", () => {
    const { server, offline, other } = openDocuments();
    const text = (offline.getXmlFragment(FIELD).get(2) as Y.XmlElement).get(0) as Y.XmlText;
    text.insert(text.length, " [kept]");
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, offline);
    deliver(offline, server, "connection");
    deliver(server, other);
    expect(texts(server)).toEqual(["Alpha", "Bravo (AI)", "Charlie [kept]"]);
    expect(texts(offline)).toEqual(texts(server));
  });
});
