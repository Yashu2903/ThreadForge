/**
 * documents.seed_format = 'tiptap' (an AI draft): the collaboration service seeds the first Yjs state from
 * validated Tiptap JSON, diagrams included, through the same seeding path as every other document; and the
 * LIVE document's snapshots use the "structure" diagram policy (a half-typed source must not break storing).
 */
import { type PMJson, YJS_FIELD, getEditorSchema } from "@threadforge/editor-schema";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";
import { buildSeed, validatedSnapshot } from "../src/documents";
import { DEFAULT_LIMITS } from "@threadforge/editor-schema";
import { all, createDocument, scene } from "./fixtures";
import { connect, docJson, startCollab, stopAll, until } from "./harness";

afterEach(stopAll);

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const SOURCE = "flowchart TD\n  A[Start] --> B[End]";
const draft = (source = SOURCE): PMJson => ({
  type: "doc",
  content: [
    { type: "heading", attrs: { id: id(1), level: 2 }, content: [{ type: "text", text: "Overview" }] },
    { type: "paragraph", attrs: { id: id(2) }, content: [{ type: "text", text: "Bold start", marks: [{ type: "bold" }] }] },
    { type: "diagram", attrs: { id: id(3) }, content: [{ type: "text", text: source }] },
  ],
});

describe("seed_format tiptap", () => {
  it("seeds a draft with formatting and a diagram, and the diagram text is character-level Yjs text", async () => {
    const server = await startCollab();
    const s = await scene();
    const docId = await createDocument(s.roomId, s.owner, draft(), "Draft", "tiptap");
    const client = connect(server, s.owner, docId);
    await client.ready;
    const json = docJson(client.ydoc);
    expect(json.content?.map((n) => n.type)).toEqual(["heading", "paragraph", "diagram"]);
    expect(json.content![1]!.content![0]!.marks).toEqual([{ type: "bold" }]);
    const diagram = client.ydoc.getXmlFragment(YJS_FIELD).get(2) as Y.XmlElement;
    expect((diagram.get(0) as Y.XmlText).toString()).toBe(SOURCE);
    const [row] = await all<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [docId]);
    expect(row!.collab_state).toBe("ready");
  });

  it("refuses a hostile diagram at seeding: the document is not seeded and nothing unsafe is stored", async () => {
    const server = await startCollab();
    const s = await scene();
    const hostile = draft('flowchart TD\n  A --> B\n  click A href "javascript:alert(1)"');
    const docId = await createDocument(s.roomId, s.owner, hostile, "Bad", "tiptap");
    const client = connect(server, s.owner, docId);
    await expect(client.ready).rejects.toBeDefined();
    const state = await all("SELECT 1 FROM document_yjs_state WHERE document_id = $1", [docId]);
    expect(state).toHaveLength(0);
  });

  it("an interchange document is unchanged by the new column", async () => {
    const server = await startCollab();
    const s = await scene();
    const [row] = await all<{ seed_format: string }>("SELECT seed_format FROM documents WHERE id = $1", [s.docId]);
    expect(row!.seed_format).toBe("interchange");
    const client = connect(server, s.owner, s.docId);
    await client.ready;
    expect(docJson(client.ydoc).content?.length).toBeGreaterThan(0);
  });

  it("stores a live document whose diagram source is half-typed (structure policy), and strict validation still refuses it", async () => {
    const server = await startCollab();
    const s = await scene();
    const docId = await createDocument(s.roomId, s.owner, draft(), "Typing", "tiptap");
    const client = connect(server, s.owner, docId);
    await client.ready;
    const diagram = client.ydoc.getXmlFragment(YJS_FIELD).get(2) as Y.XmlElement;
    const text = diagram.get(0) as Y.XmlText;
    const counter = async () =>
      Number((await all<{ c: string }>("SELECT update_counter::text AS c FROM document_yjs_state WHERE document_id = $1", [docId]))[0]!.c);
    const before = await counter();
    text.insert(text.length, "\n  C[x < y]"); // a `<` in a label: refused by the strict rules, fine while typing
    await until(async () => (await counter()) > before, "the half-typed state is stored");
    const [st] = await all<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [docId]);
    expect(st!.collab_state).toBe("ready");
    expect(getEditorSchema().nodes.diagram).toBeDefined();
  });
});

describe("validatedSnapshot / buildSeed diagram policies", () => {
  it("snapshots accept a half-typed source; seeding refuses it", async () => {
    const half = draft("flowchart TD\n  A[x < y] --> B");
    expect(() => buildSeed("tiptap", half, DEFAULT_LIMITS)).toThrow(/diagram_html_not_allowed/);
    const ydoc = buildSeed("tiptap", draft(), DEFAULT_LIMITS).ydoc;
    const diagram = ydoc.getXmlFragment(YJS_FIELD).get(2) as Y.XmlElement;
    (diagram.get(0) as Y.XmlText).insert(0, "x < ");
    const outcome = await validatedSnapshot(ydoc, DEFAULT_LIMITS);
    expect(outcome.ok).toBe(true);
  });
});
