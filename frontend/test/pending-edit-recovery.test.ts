/**
 * The fix for docs/ai.md 19.6: text this browser typed but the server never acknowledged is kept when a merge, a
 * switch, a page close or a permission change would destroy it, and it is never merged back automatically.
 *
 * Real Yjs documents (the structure the editor and the server produce) and the real CollabSession with an inert
 * socket. Delivery between documents is explicit, so every ordering is deterministic. The collaboration service's own
 * apply code is exercised against real Hocuspocus and PostgreSQL in collab/test/pending-recovery.test.ts and the
 * browser path in e2e/ai-proposals.spec.ts.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import { PendingTracker } from "../src/collab/pending";
import { CollabSession, type RecoveryCapture } from "../src/collab/session";
import { FIELD, aiReplace, deliver, openDocuments, paragraph, texts } from "./yjs-fixtures";

/** A tracker the way CollabSession wires it: it listens to REMOTE transactions and keeps what they destroy. */
function watch(doc: Y.Doc) {
  const tracker = new PendingTracker(doc);
  const lost: ReturnType<PendingTracker["lostIn"]> = [];
  doc.on("afterTransaction", (tr: Y.Transaction) => {
    if (!tr.local) lost.push(...tracker.lostIn(tr));
  });
  return { tracker, lost };
}

const typeAtEnd = (doc: Y.Doc, block: number, text: string) => {
  const t = (doc.getXmlFragment(FIELD).get(block) as Y.XmlElement).get(0) as Y.XmlText;
  t.insert(t.length, text);
};

describe("an unsent edit inside a block the AI replaced", () => {
  it("is kept: the shared document takes the AI's text and the words are in the recovery copy", () => {
    const { server, offline, other } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge(); // opened and synced: everything so far is the server's
    typeAtEnd(offline, 1, " [offline note]");
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, other);
    deliver(server, offline); // the provider delivers the server's changes first...
    deliver(offline, server, "connection"); // ...then sends its own
    deliver(server, other);

    expect(texts(server)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]); // the accepted AI change is intact
    expect(texts(offline)).toEqual(texts(server));
    expect(texts(other)).toEqual(texts(server));
    expect(lost).toHaveLength(1);
    expect(lost[0]).toMatchObject({ blockId: "b-2", inserted: " [offline note]", yourVersion: "Bravo [offline note]" });
    // not restored by itself: the old text is nowhere in the shared document
    expect(JSON.stringify(server.getXmlFragment(FIELD).toJSON())).not.toContain("offline note");
  });

  it("is kept when the delayed update reaches the server BEFORE the server's changes reach the browser", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    typeAtEnd(offline, 1, " [late]");
    aiReplace(server, 1, "Bravo (AI)");
    deliver(offline, server, "connection"); // arrives after the replacement: deleted on arrival at the server
    expect(texts(server)[1]).toBe("Bravo (AI)");
    deliver(server, offline);
    expect(lost.map((p) => p.inserted)).toEqual([" [late]"]);
    expect(texts(offline)).toEqual(texts(server));
  });

  it("does not report text the server had already acknowledged", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    typeAtEnd(offline, 1, " [sent]");
    deliver(offline, server, "connection");
    tracker.acknowledge(); // the server acknowledged it
    aiReplace(server, 1, "Bravo (AI)"); // a later replacement
    deliver(server, offline);
    expect(lost).toEqual([]);
  });

  it("keeps only the unacknowledged part when sent and unsent characters were typed in one run", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    typeAtEnd(offline, 1, " sent");
    deliver(offline, server, "connection");
    tracker.acknowledge();
    typeAtEnd(offline, 1, " unsent"); // merges into the same Yjs item as " sent"
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, offline);
    expect(lost.map((p) => p.inserted)).toEqual([" unsent"]);
  });

  it("is kept when the whole block is deleted", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    typeAtEnd(offline, 1, " [offline note]");
    server.transact(() => server.getXmlFragment(FIELD).delete(1, 1), { source: "local", context: { source: "ai-apply" } });
    deliver(server, offline);
    expect(texts(offline)).toEqual(["Alpha", "Charlie"]);
    expect(lost).toHaveLength(1);
    expect(lost[0]).toMatchObject({ blockId: "b-2", inserted: " [offline note]" });
  });

  it("is kept when a whole new paragraph typed offline sits inside a block the AI deleted", () => {
    const { server, offline } = openDocuments();
    const quote = new Y.XmlElement("blockquote");
    quote.setAttribute("id", "q-1");
    server.getXmlFragment(FIELD).insert(3, [quote]);
    quote.insert(0, [paragraph("q-p-1", "Quoted")]);
    deliver(server, offline);
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    // offline: a NEW paragraph inside the quote (a whole node, not just characters)
    const mine = (offline.getXmlFragment(FIELD).get(3) as Y.XmlElement);
    mine.insert(1, [paragraph("n-1", "typed offline in a quote")]);
    server.transact(() => server.getXmlFragment(FIELD).delete(3, 1), { source: "local", context: { source: "ai-apply" } });
    deliver(server, offline);
    expect(texts(offline)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(lost).toHaveLength(1);
    expect(lost[0]).toMatchObject({ blockId: "n-1", inserted: "typed offline in a quote" });
  });

  it("does not report anything when the server only deletes a neighbouring block", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    typeAtEnd(offline, 2, " [kept]");
    server.transact(() => server.getXmlFragment(FIELD).delete(0, 1));
    deliver(server, offline);
    expect(lost).toEqual([]);
    expect(texts(offline)).toEqual(["Bravo", "Charlie [kept]"]);
  });

  it("leaves an offline edit in a block the AI did not touch alone", () => {
    const { server, offline, other } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    typeAtEnd(offline, 2, " [kept]");
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, offline);
    deliver(offline, server, "connection");
    deliver(server, other);
    expect(texts(server)).toEqual(["Alpha", "Bravo (AI)", "Charlie [kept]"]);
    expect(lost).toEqual([]);
  });

  it("keeps each disconnected browser's own text, and only that", () => {
    const { server, offline, other } = openDocuments();
    const a = watch(offline);
    const b = watch(other);
    a.tracker.acknowledge();
    b.tracker.acknowledge();
    typeAtEnd(offline, 1, " [from A]");
    typeAtEnd(other, 1, " [from B]");
    aiReplace(server, 1, "Bravo (AI)");
    for (const doc of [offline, other]) {
      deliver(server, doc);
      deliver(doc, server, "connection");
    }
    deliver(server, offline);
    deliver(server, other);
    expect(texts(server)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    expect(texts(offline)).toEqual(texts(server));
    expect(texts(other)).toEqual(texts(server));
    expect(a.lost.map((p) => p.inserted)).toEqual([" [from A]"]);
    expect(b.lost.map((p) => p.inserted)).toEqual([" [from B]"]);
  });

  it("does not capture the same text again on repeated reconnects", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    typeAtEnd(offline, 1, " [once]");
    aiReplace(server, 1, "Bravo (AI)");
    for (let i = 0; i < 4; i += 1) {
      deliver(server, offline);
      deliver(offline, server, "connection");
    }
    aiReplace(server, 0, "Alpha (AI)"); // another later change from the server
    deliver(server, offline);
    expect(lost.map((p) => p.inserted)).toEqual([" [once]"]);
    expect(tracker.alive()).toEqual([]); // and nothing is left that could be captured a second time
  });

  it("ignores text the person typed and then deleted themselves, and whitespace-only changes", () => {
    const { server, offline } = openDocuments();
    const { tracker, lost } = watch(offline);
    tracker.acknowledge();
    const text = (offline.getXmlFragment(FIELD).get(1) as Y.XmlElement).get(0) as Y.XmlText;
    text.insert(text.length, "oops");
    text.delete(text.length - 4, 4);
    text.insert(text.length, "  ");
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, offline);
    expect(lost).toEqual([]);
  });
});

describe("what would be lost if this browser went away now", () => {
  it("reports unacknowledged text that is still in the document, once", () => {
    const { offline } = openDocuments();
    const { tracker } = watch(offline);
    tracker.acknowledge();
    expect(tracker.alive()).toEqual([]);
    typeAtEnd(offline, 1, " [unsent]");
    const first = tracker.alive();
    expect(first).toHaveLength(1);
    expect(first[0]).toMatchObject({ blockId: "b-2", inserted: " [unsent]", yourVersion: "Bravo [unsent]" });
    expect(tracker.alive()).toEqual([]); // already handed out: access loss followed by teardown does not duplicate it
  });

  it("reports nothing once the server acknowledged everything", () => {
    const { offline } = openDocuments();
    const { tracker } = watch(offline);
    typeAtEnd(offline, 1, " [sent]");
    tracker.acknowledge();
    expect(tracker.alive()).toEqual([]);
  });
});

// ---- the real CollabSession, with an inert socket -------------------------------------------------

class InertSocket {
  readyState = 0;
  onopen: unknown = null;
  onclose: unknown = null;
  onmessage: unknown = null;
  onerror: unknown = null;
  addEventListener() {}
  removeEventListener() {}
  send() {}
  close() {
    this.readyState = 3;
  }
}

function session(onRecover: (c: RecoveryCapture) => void) {
  const s = new CollabSession({ url: "ws://localhost/collab", documentName: "doc:test", canEdit: true, WebSocketPolyfill: InertSocket, onRecover });
  // the document as the server delivers it, then "synced and acknowledged"
  const server = new Y.Doc();
  server.getXmlFragment(FIELD).insert(0, [paragraph("b-1", "Alpha"), paragraph("b-2", "Bravo")]);
  deliver(server, s.ydoc);
  const config = (s.provider as unknown as { configuration: Record<string, (arg: unknown) => void> }).configuration;
  config.onUnsyncedChanges!({ number: 0 });
  return { s, server, config };
}

const live: CollabSession[] = [];
afterEach(() => {
  for (const s of live.splice(0)) s.destroy();
});

describe("CollabSession", () => {
  it("hands over the lost text when the server's replacement arrives", () => {
    const captures: RecoveryCapture[] = [];
    const { s, server } = session((c) => captures.push(c));
    live.push(s);
    typeAtEnd(s.ydoc, 1, " [offline note]");
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, s.ydoc);
    expect(captures).toHaveLength(1);
    expect(captures[0]).toMatchObject({ reason: "replaced_while_pending" });
    expect(captures[0]!.pieces[0]).toMatchObject({ blockId: "b-2", inserted: " [offline note]" });
    expect(captures[0]!.captureId).toMatch(/^\d+:\d+-\d+$/);
  });

  it("records which AI proposals the document had applied", () => {
    const captures: RecoveryCapture[] = [];
    const { s, server } = session((c) => captures.push(c));
    live.push(s);
    typeAtEnd(s.ydoc, 1, " [x]");
    server.transact(() => server.getMap("ai_applications").set("proposal-1", { at: 1 }));
    aiReplace(server, 1, "Bravo (AI)");
    deliver(server, s.ydoc);
    expect(captures[0]!.appliedProposals).toEqual(["proposal-1"]);
  });

  it("keeps unsent text when the session is destroyed (document switch, room switch, unmount)", () => {
    const captures: RecoveryCapture[] = [];
    const { s } = session((c) => captures.push(c));
    typeAtEnd(s.ydoc, 1, " [unsent]");
    s.destroy();
    expect(captures).toHaveLength(1);
    expect(captures[0]).toMatchObject({ reason: "left_with_unsent_changes" });
    expect(captures[0]!.pieces[0]!.inserted).toBe(" [unsent]");
    s.destroy(); // idempotent
    expect(captures).toHaveLength(1);
  });

  it("keeps nothing when everything was acknowledged", () => {
    const captures: RecoveryCapture[] = [];
    const { s, config } = session((c) => captures.push(c));
    typeAtEnd(s.ydoc, 1, " [sent]");
    config.onUnsyncedChanges!({ number: 0 });
    s.destroy();
    expect(captures).toEqual([]);
  });

  it("keeps unsent text when access is lost, before anything can free the document, and not twice", () => {
    const captures: RecoveryCapture[] = [];
    const { s, config } = session((c) => captures.push(c));
    typeAtEnd(s.ydoc, 1, " [unsent]");
    config.onAuthenticationFailed!({ reason: "forbidden" });
    expect(captures.map((c) => c.reason)).toEqual(["access_lost"]);
    s.destroy(); // the screen that replaces the editor frees the document: no duplicate copy
    expect(captures).toHaveLength(1);
  });

  it("keeps unsent text when the person is made read-only", () => {
    const captures: RecoveryCapture[] = [];
    const { s, config } = session((c) => captures.push(c));
    live.push(s);
    typeAtEnd(s.ydoc, 1, " [unsent]");
    config.onStateless!({ payload: JSON.stringify({ type: "read_only" }) });
    expect(captures.map((c) => c.reason)).toEqual(["became_read_only"]);
  });

  it("keeps unsent text when the page is closed, but not when it only goes into the back/forward cache", () => {
    const captures: RecoveryCapture[] = [];
    const { s } = session((c) => captures.push(c));
    live.push(s);
    typeAtEnd(s.ydoc, 1, " [unsent]");
    window.dispatchEvent(Object.assign(new Event("pagehide"), { persisted: true }));
    expect(captures).toEqual([]);
    window.dispatchEvent(Object.assign(new Event("pagehide"), { persisted: false }));
    expect(captures.map((c) => c.reason)).toEqual(["left_with_unsent_changes"]);
  });

  it("removes its page listener and stops reporting once destroyed", () => {
    const captures: RecoveryCapture[] = [];
    const remove = vi.spyOn(window, "removeEventListener");
    const { s, server } = session((c) => captures.push(c));
    s.destroy();
    expect(remove).toHaveBeenCalledWith("pagehide", expect.any(Function));
    remove.mockRestore();
    aiReplace(server, 1, "Bravo (AI)");
    window.dispatchEvent(Object.assign(new Event("pagehide"), { persisted: false }));
    expect(captures).toEqual([]);
  });
});
