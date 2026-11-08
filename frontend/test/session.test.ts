import { describe, expect, it } from "vitest";
import type { Doc } from "../src/editor/doc";
import { AUTOSAVE_MS, DocumentSession, type EditorLike } from "../src/documents/session";
import { type Call, ROOM, csrfRoute, fakeApi, flush, full } from "./helpers";

const DOC = "d1111111-1111-4111-8111-111111111111";
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const docOf = (text: string): Doc => ({
  schema_version: 1,
  blocks: [{ type: "paragraph", id: id(1), ...(text ? { children: [{ type: "text", text }] } : {}) }],
});

class Clock {
  t = 1000;
  timers: { at: number; fn: () => void }[] = [];
  now = () => this.t;
  setTimeout = (fn: () => void, ms: number) => {
    const timer = { at: this.t + ms, fn };
    this.timers.push(timer);
    return timer;
  };
  clearTimeout = (h: unknown) => {
    this.timers = this.timers.filter((t) => t !== h);
  };
  async advance(ms: number) {
    const target = this.t + ms;
    for (;;) {
      const due = this.timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      this.timers = this.timers.filter((t) => t !== due);
      this.t = due.at;
      due.fn();
      await flush();
    }
    this.t = target;
    await flush();
  }
}

class StubEditor implements EditorLike {
  doc: Doc;
  isComposing = false;
  readOnly = false;
  adopted: Doc[] = [];
  constructor(doc: Doc) {
    this.doc = doc;
  }
  getDocument() {
    return this.doc;
  }
  setDocument(doc: Doc) {
    this.doc = doc;
    this.adopted.push(doc);
  }
  setReadOnly(v: boolean) {
    this.readOnly = v;
  }
}

type Reply = { status?: number; body?: unknown; headers?: Record<string, string> };

function setup(replies: (call: Call, n: number) => Reply | Promise<Reply> = (_c, n) => ({ body: { content_version: 1 + n, unchanged: false, canonical_changed: false, content: null } })) {
  const clock = new Clock();
  let n = 0;
  const { api, calls } = fakeApi({
    ...csrfRoute,
    [`PUT /rooms/${ROOM}/documents/${DOC}/content`]: (call) => replies(call, ++n),
  });
  const initial = full(DOC, "Doc", { content: docOf("start") as never, content_version: 1 });
  const session = new DocumentSession({ api, roomId: ROOM, initial, deps: clock });
  const editor = new StubEditor(docOf("start"));
  editor.doc = initial.content as unknown as Doc;
  session.attach(editor);
  const type = (text: string) => {
    editor.doc = docOf(text);
    session.onEditorChange();
  };
  const puts = () => calls.filter((c) => c.method === "PUT");
  return { session, editor, clock, calls, puts, type, initial };
}

const conflict409 = (version: number): Reply => ({ status: 409, body: { error: { code: "document_version_conflict", message: "x", current_content_version: version } } });

describe("autosave", () => {
  it("is unsaved right after an edit, saves once the edits settle, and is saved only after the server committed", async () => {
    const { session, clock, puts, type } = setup();
    type("a");
    expect(session.getSnapshot().status).toBe("unsaved");
    await clock.advance(AUTOSAVE_MS - 1);
    expect(puts()).toHaveLength(0);
    type("ab"); // another edit restarts the settle timer
    await clock.advance(AUTOSAVE_MS - 1);
    expect(puts()).toHaveLength(0);
    await clock.advance(2);
    expect(puts()).toHaveLength(1);
    expect(puts()[0]!.body).toMatchObject({ schema_version: 1, expected_content_version: 1 });
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 2, dirty: false });
  });

  it("an explicit save does not wait for the timer and cancels it", async () => {
    const { session, clock, puts, type } = setup();
    type("x");
    await session.saveNow();
    expect(puts()).toHaveLength(1);
    await clock.advance(5000);
    expect(puts()).toHaveLength(1);
  });

  it("saving a clean document sends nothing", async () => {
    const { session, puts } = setup();
    await session.saveNow();
    expect(puts()).toHaveLength(0);
    expect(session.getSnapshot().status).toBe("saved");
  });

  it("never saves during IME composition: the timer waits until it ends", async () => {
    const { clock, puts, type, editor } = setup();
    type("に");
    editor.isComposing = true;
    await clock.advance(AUTOSAVE_MS + 2000);
    expect(puts()).toHaveLength(0);
    editor.isComposing = false;
    await clock.advance(400);
    expect(puts()).toHaveLength(1);
  });

  it("one save in flight: typing during it keeps the newer text and the next save uses the returned version", async () => {
    let release!: () => void;
    const { session, clock, puts, type, editor } = setup((call, n) =>
      n === 1
        ? new Promise((res) => (release = () => res({ body: { content_version: 2, unchanged: false, canonical_changed: false, content: null } })))
        : { body: { content_version: 3, unchanged: false, canonical_changed: false, content: null } },
    );
    type("first");
    const first = session.saveNow();
    await flush();
    await flush();
    expect(session.getSnapshot().status).toBe("saving");
    type("first and more"); // typed while the request is in flight
    release();
    await first;
    await flush();
    expect(session.getSnapshot()).toMatchObject({ status: "unsaved", contentVersion: 2, dirty: true });
    expect(editor.doc).toEqual(docOf("first and more")); // newer local text survived
    await clock.advance(AUTOSAVE_MS + 10);
    expect(puts()).toHaveLength(2);
    expect(puts()[0]!.body).toMatchObject({ expected_content_version: 1 });
    expect(puts()[1]!.body).toMatchObject({ expected_content_version: 2, content: docOf("first and more") });
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 3 });
  });

  it("an explicit save requested while one is in flight runs right after it", async () => {
    let release!: () => void;
    const { session, puts, type } = setup((_c, n) =>
      n === 1
        ? new Promise((res) => (release = () => res({ body: { content_version: 2, unchanged: false, canonical_changed: false, content: null } })))
        : { body: { content_version: 3, unchanged: false, canonical_changed: false, content: null } },
    );
    type("one");
    const first = session.saveNow();
    await flush();
    await flush();
    type("one two");
    void session.saveNow(); // Ctrl+S while saving: still only one request at a time
    expect(puts()).toHaveLength(1);
    release();
    await first;
    await flush();
    expect(puts()).toHaveLength(2);
    expect(puts()[1]!.body).toMatchObject({ expected_content_version: 2 });
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 3 });
  });

  it("unchanged:true from the server counts as saved", async () => {
    const { session, type } = setup(() => ({ body: { content_version: 1, unchanged: true, canonical_changed: false, content: null } }));
    type("same");
    await session.saveNow();
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 1 });
  });

  it("adopts the server's canonical content only when there are no newer local edits", async () => {
    const canonical = docOf("canon");
    const { session, editor, type } = setup(() => ({ body: { content_version: 2, unchanged: false, canonical_changed: true, content: canonical } }));
    type("raw");
    await session.saveNow();
    expect(editor.adopted).toEqual([canonical]);
    expect(session.getSnapshot().dirty).toBe(false);

    let release!: () => void;
    const second = setup((_c, n) => (n === 1 ? new Promise((res) => (release = () => res({ body: { content_version: 2, unchanged: false, canonical_changed: true, content: canonical } }))) : { body: { content_version: 3, unchanged: false, canonical_changed: false, content: null } }));
    second.type("raw");
    const pending = second.session.saveNow();
    await flush();
    await flush();
    second.type("raw and typed on");
    release();
    await pending;
    expect(second.editor.adopted).toEqual([]); // not replaced: that would erase the typing
    expect(second.editor.doc).toEqual(docOf("raw and typed on"));
  });

  it("a document the client cannot canonicalize is not sent", async () => {
    const { session, puts, type, editor } = setup();
    editor.doc = { schema_version: 1, blocks: [] } as never;
    session.onEditorChange();
    await session.saveNow();
    expect(puts()).toHaveLength(0);
    expect(session.getSnapshot()).toMatchObject({ status: "failed" });
    expect(session.getSnapshot().message).toMatch(/cannot be saved/);
    void type;
  });
});

describe("failures", () => {
  it("a network failure keeps the text, backs off 2s then 4s, and a manual retry works", async () => {
    let fail = 2;
    const { session, clock, puts, type } = setup(() => {
      if (fail-- > 0) throw new TypeError("network down");
      return { body: { content_version: 2, unchanged: false, canonical_changed: false, content: null } };
    });
    type("keep me");
    await session.saveNow();
    expect(session.getSnapshot()).toMatchObject({ status: "failed", dirty: true });
    expect(session.getSnapshot().message).toMatch(/trying again in 2 seconds/);
    await clock.advance(2000);
    expect(puts()).toHaveLength(2);
    expect(session.getSnapshot().message).toMatch(/in 4 seconds/);
    session.retryNow(); // do not wait
    await flush();
    await flush();
    expect(puts()).toHaveLength(3);
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 2 });
  });

  it("429 pauses autosave for Retry-After, then tries again", async () => {
    let limited = true;
    const { session, clock, puts, type } = setup(() => {
      if (limited) {
        limited = false;
        return { status: 429, body: { error: { code: "rate_limited", message: "slow" } }, headers: { "Retry-After": "7" } };
      }
      return { body: { content_version: 2, unchanged: false, canonical_changed: false, content: null } };
    });
    type("busy");
    await session.saveNow();
    expect(session.getSnapshot().message).toMatch(/paused for 7 seconds/);
    type("busy again"); // edits during the pause do not trigger early attempts
    await clock.advance(6000);
    expect(puts()).toHaveLength(1);
    await clock.advance(1100);
    expect(puts()).toHaveLength(2);
    expect(session.getSnapshot().status).toBe("saved");
  });

  it("a 422 is reported and does not loop", async () => {
    const { session, clock, puts, type } = setup(() => ({ status: 422, body: { error: { code: "invalid_document_content", message: "The document content is not valid." } } }));
    type("bad");
    await session.saveNow();
    await clock.advance(120_000);
    expect(puts()).toHaveLength(1);
    expect(session.getSnapshot()).toMatchObject({ status: "failed", dirty: true });
  });
});

describe("conflicts", () => {
  it("409 stops saving, keeps the local text and explains; editing carries on without saving", async () => {
    const { session, clock, puts, type, editor } = setup(() => conflict409(5));
    type("mine");
    await session.saveNow();
    expect(session.getSnapshot()).toMatchObject({ status: "conflict", serverVersion: 5, dirty: true });
    expect(session.getSnapshot().message).toMatch(/Another session saved a newer version/);
    type("mine, edited more");
    await clock.advance(60_000);
    expect(puts()).toHaveLength(1);
    expect(editor.doc).toEqual(docOf("mine, edited more"));
    expect(editor.adopted).toEqual([]); // no automatic merge, no automatic overwrite
    await session.saveNow();
    expect(puts()).toHaveLength(1); // not even an explicit save: there is no force
  });

  it("an event for a newer version while holding unsaved work is a conflict immediately (no 409 needed)", async () => {
    const { session, puts, type } = setup();
    type("dirty");
    session.onServerEvent(2);
    expect(session.getSnapshot()).toMatchObject({ status: "conflict", serverVersion: 2 });
    expect(puts()).toHaveLength(0);
  });

  it("events at or below the version we hold are our own saves and are ignored", async () => {
    const { session, type } = setup();
    type("a");
    await session.saveNow(); // now at version 2
    type("b");
    session.onServerEvent(2);
    session.onServerEvent(1);
    expect(session.getSnapshot().status).toBe("unsaved");
  });

  it("an event for our own save that arrives BEFORE the HTTP answer is not a conflict", async () => {
    let release!: () => void;
    const { session, type } = setup(() => new Promise((res) => (release = () => res({ body: { content_version: 2, unchanged: false, canonical_changed: false, content: null } }))));
    type("mine");
    const saving = session.saveNow();
    await flush();
    await flush();
    session.onServerEvent(2); // the realtime event beat the response
    expect(session.getSnapshot().status).toBe("saving");
    release();
    await saving;
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 2, serverVersion: null });
  });

  it("an event for SOMEONE ELSE's later version during a save is judged after the answer", async () => {
    let release!: () => void;
    const { session, type } = setup(() => new Promise((res) => (release = () => res({ body: { content_version: 2, unchanged: false, canonical_changed: false, content: null } }))));
    type("mine");
    const saving = session.saveNow();
    await flush();
    await flush();
    type("mine and more");
    session.onServerEvent(3);
    release();
    await saving;
    expect(session.getSnapshot()).toMatchObject({ status: "conflict", serverVersion: 3 });
  });

  it("a clean copy adopts a newer server version; history is reset by setDocument", async () => {
    const { session, editor } = setup();
    session.onServerEvent(4);
    expect(session.getSnapshot().status).toBe("saved");
    const newer = full(DOC, "Doc", { content: docOf("from someone else") as never, content_version: 4 });
    session.onServerDocument(newer);
    expect(editor.adopted).toEqual([newer.content]);
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 4, dirty: false });
    expect(session.getSnapshot().message).toMatch(/latest version/);
    session.onServerDocument(newer); // repeats change nothing
    expect(editor.adopted).toHaveLength(1);
  });

  it("a dirty copy NEVER adopts a newer server version: it becomes a conflict instead", async () => {
    const { session, editor, type } = setup();
    type("my work");
    session.onServerDocument(full(DOC, "Doc", { content: docOf("theirs") as never, content_version: 3 }));
    expect(editor.adopted).toEqual([]);
    expect(editor.doc).toEqual(docOf("my work"));
    expect(session.getSnapshot()).toMatchObject({ status: "conflict", serverVersion: 3 });
  });

  it("Load latest discards the local edits only when called, and leaves a clean saved state", async () => {
    const { session, editor, type, puts, clock } = setup();
    type("my work");
    session.onServerEvent(3);
    const theirs = full(DOC, "Doc", { content: docOf("theirs") as never, content_version: 3 });
    session.loadLatest(theirs);
    expect(editor.doc).toEqual(docOf("theirs"));
    expect(session.getSnapshot()).toMatchObject({ status: "saved", contentVersion: 3, serverVersion: null, dirty: false });
    type("continuing");
    await clock.advance(AUTOSAVE_MS + 10);
    expect(puts()[0]!.body).toMatchObject({ expected_content_version: 3 }); // saving works again from the new base
  });

  it("Copy my text returns the local document as plain text", () => {
    const { session, editor } = setup();
    editor.doc = {
      schema_version: 1,
      blocks: [
        { type: "paragraph", id: id(1), children: [{ type: "text", text: "line one" }, { type: "hard_break" }, { type: "text", text: "line two" }] },
        { type: "paragraph", id: id(2), children: [{ type: "text", text: "para two" }] },
      ],
    };
    expect(session.localText()).toBe("line one\nline two\npara two");
  });
});

describe("losing edit rights", () => {
  it("a 403 on save (role downgrade) stops saving, locks the editor and keeps the text", async () => {
    const { session, editor, clock, puts, type } = setup(() => ({ status: 403, body: { error: { code: "insufficient_role", message: "no" } } }));
    type("unsaved words");
    await session.saveNow();
    expect(session.getSnapshot()).toMatchObject({ status: "read-only", readOnlyReason: "role_changed", dirty: true });
    expect(editor.readOnly).toBe(true);
    await clock.advance(60_000);
    expect(puts()).toHaveLength(1);
    expect(session.localText()).toBe("unsaved words");
    await session.saveNow();
    expect(puts()).toHaveLength(1);
  });

  it("removal (404) is access lost; setReadOnly() from the room state works without any request", async () => {
    const { session, editor, puts, type } = setup(() => ({ status: 404, body: { error: { code: "room_not_found", message: "gone" } } }));
    type("x");
    await session.saveNow();
    expect(session.getSnapshot().readOnlyReason).toBe("access_lost");
    const other = setup();
    other.type("typed");
    other.session.setReadOnly("role_changed");
    other.session.onEditorChange();
    await other.session.saveNow();
    other.session.onServerEvent(9);
    expect(other.puts()).toHaveLength(0);
    expect(other.editor.readOnly).toBe(true);
    expect(other.session.getSnapshot()).toMatchObject({ status: "read-only", dirty: true });
    expect(puts()).toHaveLength(1);
    expect(editor).toBeTruthy();
  });

  it("regaining edit rights resumes: unsaved work is saved again", async () => {
    const { session, clock, puts, type } = setup();
    type("kept");
    session.setReadOnly("role_changed");
    session.setReadOnly(null);
    expect(session.getSnapshot().status).toBe("unsaved");
    await clock.advance(AUTOSAVE_MS + 10);
    expect(puts()).toHaveLength(1);
  });

  it("a viewer or a mobile browser starts read-only", () => {
    const { session, editor } = setup();
    session.setReadOnly("mobile");
    expect(editor.readOnly).toBe(true);
    expect(session.getSnapshot().message).toMatch(/mobile/);
  });

  it("hasUnsavedWork covers dirty and in-flight; dispose stops everything", async () => {
    const { session, clock, puts, type } = setup();
    expect(session.hasUnsavedWork).toBe(false);
    type("x");
    expect(session.hasUnsavedWork).toBe(true);
    session.dispose();
    await clock.advance(10_000);
    expect(puts()).toHaveLength(0);
  });
});
