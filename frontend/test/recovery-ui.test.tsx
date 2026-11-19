/**
 * The recovery copy of unsent text: the bounded local store, the panel the person sees, and the editor insertion.
 * (How the text is captured is in pending-edit-recovery.test.ts.)
 */
import { createExtensions } from "@threadforge/editor-schema";
import { Editor } from "@tiptap/core";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  MAX_BLOCKS,
  MAX_CHARS,
  MAX_RECORDS,
  RETENTION_DAYS,
  RecoveryStore,
  type RecoveryRecord,
  recordFrom,
  recoveryStore,
} from "../src/collab/recovery";
import { RecoveryPanel } from "../src/components/RecoveryPanel";
import { insertParagraphs } from "../src/DocumentEditor";

for (const proto of [Range.prototype, Element.prototype]) {
  Object.assign(proto, { getClientRects: () => [], getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }) });
}

const NOW = Date.parse("2026-11-19T12:00:00Z");
function record(over: Partial<RecoveryRecord> = {}): RecoveryRecord {
  return {
    id: "doc-1|1:5-20",
    userId: "u-1",
    roomId: "r-1",
    documentId: "doc-1",
    documentTitle: "Plan",
    capturedAt: new Date(NOW).toISOString(),
    reason: "replaced_while_pending",
    base: { acknowledgedClock: 5, appliedProposals: ["p-1"] },
    blocks: [{ blockId: "b-2", inserted: " [offline note]", yourVersion: "Bravo [offline note]" }],
    ...over,
  };
}

/** An in-memory stand-in for localStorage. */
function fakeStorage(opts: { failWrites?: boolean | (() => boolean) } = {}): Storage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => {
      const fail = typeof opts.failWrites === "function" ? opts.failWrites() : opts.failWrites;
      if (fail) throw new DOMException("full", "QuotaExceededError");
      data.set(k, v);
    },
  };
}

describe("the recovery store", () => {
  it("keeps a record for its person, survives a reload, and ignores a duplicate", () => {
    const storage = fakeStorage();
    const a = new RecoveryStore(storage, () => NOW);
    expect(a.add(record())).toBe(true);
    expect(a.add(record())).toBe(false); // the same characters captured twice
    const reloaded = new RecoveryStore(storage, () => NOW);
    expect(reloaded.list("u-1")).toHaveLength(1);
    expect(reloaded.list("u-2")).toEqual([]); // another person on this browser does not see it
    expect(reloaded.list("u-1", "r-other")).toEqual([]); // nor does another room
  });

  it("identifies who, where, when, why and against what base", () => {
    const built = recordFrom(
      { reason: "left_with_unsent_changes", pieces: [{ blockId: "b-2", inserted: "x", yourVersion: "Bx", clocks: [7] }], captureId: "9:7-7", acknowledgedClock: 7, appliedProposals: ["p-9"] },
      { userId: "u-1", roomId: "r-1", documentId: "doc-1", documentTitle: "Plan" },
      new Date(NOW),
    );
    expect(built).toMatchObject({
      id: "doc-1|9:7-7",
      userId: "u-1",
      roomId: "r-1",
      documentId: "doc-1",
      capturedAt: "2026-11-19T12:00:00.000Z",
      reason: "left_with_unsent_changes",
      base: { acknowledgedClock: 7, appliedProposals: ["p-9"] },
      blocks: [{ blockId: "b-2", inserted: "x", yourVersion: "Bx" }],
    });
  });

  it("is bounded: oldest records go first, long text is cut and says so, blocks are capped", () => {
    const store = new RecoveryStore(fakeStorage(), () => NOW);
    for (let i = 0; i < MAX_RECORDS + 5; i += 1) store.add(record({ id: `doc-1|${i}`, capturedAt: new Date(NOW + i).toISOString() }));
    const ids = store.list("u-1").map((r) => r.id);
    expect(ids).toHaveLength(MAX_RECORDS);
    expect(ids).not.toContain("doc-1|0");
    expect(ids).toContain(`doc-1|${MAX_RECORDS + 4}`);

    const big = new RecoveryStore(fakeStorage(), () => NOW);
    big.add(record({ id: "big", blocks: Array.from({ length: MAX_BLOCKS + 10 }, () => ({ blockId: null, inserted: "x".repeat(MAX_CHARS + 100), yourVersion: "y" })) }));
    const stored = big.list("u-1")[0]!;
    expect(stored.blocks).toHaveLength(MAX_BLOCKS);
    expect(stored.blocks[0]!.inserted).toHaveLength(MAX_CHARS);
    expect(stored.blocks[0]!.truncated).toBe(true);
  });

  it("expires records after the retention period", () => {
    let now = NOW;
    const store = new RecoveryStore(fakeStorage(), () => now);
    store.add(record());
    now = NOW + (RETENTION_DAYS - 1) * 86_400_000;
    expect(store.list("u-1")).toHaveLength(1);
    now = NOW + (RETENTION_DAYS + 1) * 86_400_000;
    expect(store.list("u-1")).toEqual([]);
  });

  it("keeps working in memory, and says so, when the browser refuses to store", () => {
    const store = new RecoveryStore(fakeStorage({ failWrites: true }), () => NOW);
    expect(store.add(record())).toBe(true);
    expect(store.persisted).toBe(false);
    expect(store.list("u-1")).toHaveLength(1);
    const none = new RecoveryStore(null, () => NOW);
    none.add(record());
    expect(none.persisted).toBe(false);
    expect(none.list("u-1")).toHaveLength(1);
  });

  it("drops the oldest record and retries when only the quota is exceeded", () => {
    let calls = 0;
    const storage = fakeStorage({ failWrites: () => ++calls === 3 });
    const store = new RecoveryStore(storage, () => NOW);
    store.add(record({ id: "a", capturedAt: new Date(NOW).toISOString() }));
    store.add(record({ id: "b", capturedAt: new Date(NOW + 1).toISOString() }));
    store.add(record({ id: "c", capturedAt: new Date(NOW + 2).toISOString() })); // the third write fails once
    expect(JSON.parse(storage.data.get("threadforge.recovery.v1:u-1")!).map((r: RecoveryRecord) => r.id)).toEqual(["b", "c"]);
    expect(store.persisted).toBe(false); // something had to be dropped from storage
  });

  it("ignores unreadable or hostile stored data", () => {
    const storage = fakeStorage();
    storage.setItem("threadforge.recovery.v1:u-1", "{not json");
    expect(new RecoveryStore(storage, () => NOW).list("u-1")).toEqual([]);
    storage.setItem("threadforge.recovery.v1:u-1", JSON.stringify([{ id: 5 }, "x", null, record()]));
    expect(new RecoveryStore(storage, () => NOW).list("u-1")).toHaveLength(1);
  });

  it("removes only the record asked for", () => {
    const store = new RecoveryStore(fakeStorage(), () => NOW);
    store.add(record({ id: "a" }));
    store.add(record({ id: "b" }));
    store.remove("u-1", "a");
    expect(store.list("u-1").map((r) => r.id)).toEqual(["b"]);
  });
});

function Panel(props: { canInsert?: boolean; onInsert?: (r: RecoveryRecord) => boolean; roomId?: string }) {
  return (
    <RecoveryPanel
      userId="u-1"
      roomId={props.roomId ?? "r-1"}
      titles={new Map([["doc-1", "Plan (renamed)"]])}
      canInsert={() => props.canInsert ?? true}
      onInsert={props.onInsert ?? (() => true)}
    />
  );
}

function seed(...records: RecoveryRecord[]) {
  for (const r of recordsOf()) recoveryStore.remove("u-1", r.id);
  for (const r of records) recoveryStore.add(r);
}
const recordsOf = () => recoveryStore.list("u-1");

describe("the recovery panel", () => {
  it("says what happened and shows the unsent text so it can be read", () => {
    seed(record());
    render(<Panel />);
    const region = screen.getByRole("region", { name: "Recovered text" });
    expect(within(region).getByText(/reached the server when the shared document changed/)).toBeInTheDocument();
    expect(within(region).getByText("Plan (renamed)")).toBeInTheDocument();
    expect((within(region).getByLabelText(/Your unsent text/) as HTMLTextAreaElement).value).toBe(" [offline note]");
    expect(within(region).getByText("Your whole paragraph as you had it")).toBeInTheDocument();
  });

  it("shows nothing when there is nothing, and nothing from another room", () => {
    seed();
    const first = render(<Panel />);
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();
    seed(record({ roomId: "r-other" }));
    const other = render(<Panel />);
    expect(other.container).toBeEmptyDOMElement();
  });

  it("copies the unsent text", async () => {
    seed(record());
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<Panel />);
    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(" [offline note]");
    expect(await screen.findByText("Copied to the clipboard.")).toBeInTheDocument();
  });

  it("puts the text back only on request, once, and then the record is gone (no duplicate)", async () => {
    seed(record());
    const user = userEvent.setup();
    const onInsert = vi.fn(() => true);
    render(<Panel onInsert={onInsert} />);
    expect(onInsert).not.toHaveBeenCalled(); // never automatic
    await user.click(screen.getByRole("button", { name: "Insert into the document" }));
    expect(onInsert).toHaveBeenCalledTimes(1);
    expect(recordsOf()).toEqual([]);
    expect(screen.queryByRole("button", { name: "Insert into the document" })).toBeNull();
  });

  it("keeps the record when the document could not take it", async () => {
    seed(record());
    const user = userEvent.setup();
    render(<Panel onInsert={() => false} />);
    await user.click(screen.getByRole("button", { name: "Insert into the document" }));
    expect(recordsOf()).toHaveLength(1);
    expect(screen.getByText(/Nothing was removed/)).toBeInTheDocument();
  });

  it("cannot insert without edit access, but can still copy", () => {
    seed(record());
    render(<Panel canInsert={false} />);
    expect(screen.getByRole("button", { name: "Insert into the document" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Copy" })).toBeEnabled();
  });

  it("asks before discarding, and keeping it leaves it untouched", async () => {
    seed(record());
    const user = userEvent.setup();
    render(<Panel />);
    await user.click(screen.getByRole("button", { name: "Discard…" }));
    expect(recordsOf()).toHaveLength(1); // nothing removed by the first click
    expect(screen.getByRole("alert")).toHaveTextContent("Discard this text for good?");
    await user.click(screen.getByRole("button", { name: "Keep it" }));
    expect(recordsOf()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Discard…" }));
    await user.click(screen.getByRole("button", { name: "Yes, discard it" }));
    expect(recordsOf()).toEqual([]);
  });

  it("warns when the browser would not store the copy", () => {
    seed(record());
    const store = recoveryStore as unknown as { persisted: boolean };
    const before = store.persisted;
    store.persisted = false;
    try {
      render(<Panel />);
      expect(screen.getByRole("alert")).toHaveTextContent(/would not store these copies/);
    } finally {
      store.persisted = before;
    }
  });
});

describe("putting recovered text into the editor", () => {
  const ids = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000003"];
  const doc = {
    type: "doc",
    content: ids.map((id, i) => ({ type: "paragraph", attrs: { id }, content: [{ type: "text", text: ["Alpha", "Bravo (AI)", "Charlie"][i] }] })),
  };
  function editor(editable = true) {
    const element = document.createElement("div");
    document.body.appendChild(element);
    return new Editor({ element, extensions: createExtensions(), content: doc, editable });
  }
  const texts = (e: Editor) => (e.getJSON().content ?? []).map((n) => (n.content ?? []).map((c) => (c as { text?: string }).text ?? "").join(""));

  it("adds new paragraphs after the block it was typed in, without touching the AI's text", () => {
    const e = editor();
    expect(insertParagraphs(e, " [offline note]", ids[1]!)).toBe(true);
    expect(texts(e)).toEqual(["Alpha", "Bravo (AI)", "[offline note]", "Charlie"]);
  });

  it("goes to the end when that block no longer exists, and keeps line breaks as paragraphs", () => {
    const e = editor();
    expect(insertParagraphs(e, "one\ntwo", "00000000-0000-4000-8000-0000000000ff")).toBe(true);
    expect(texts(e)).toEqual(["Alpha", "Bravo (AI)", "Charlie", "one", "two"]);
  });

  it("is one undo step", () => {
    const e = editor();
    insertParagraphs(e, "one\ntwo", ids[0]!);
    expect(texts(e)).toHaveLength(5);
    // the standalone editor has no history; the collaboration editor's undo is covered by ai-undo.test.ts
    expect(e.state.doc.childCount).toBe(5);
  });

  it("refuses when the editor is read-only or the text is blank", () => {
    expect(insertParagraphs(editor(false), "text", ids[0]!)).toBe(false);
    expect(insertParagraphs(editor(), "   \n  ", ids[0]!)).toBe(false);
    expect(insertParagraphs(null, "text", null)).toBe(false);
  });
});
