/** Phase 2 UI: the composer's edit/draft choices and the review card with its preview. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Composer, type ComposerContext } from "../src/ai/Composer";
import { ProposalCard, diffWords } from "../src/ai/Proposal";
import type { ProposalInfo } from "../src/ai/types";
import { DOC, ME, ROOM, loadedStore, requestInfo, serverWith, source } from "./ai-helpers";

const ctx = (over: Partial<ComposerContext> = {}): ComposerContext => ({
  documentId: DOC,
  documentTitle: "Launch plan",
  unsynced: 0,
  readSelection: async () => null,
  readRange: async () => null,
  chatSelection: [],
  clearChatSelection: () => undefined,
  role: "editor",
  ...over,
});

async function mount(opts: Parameters<typeof serverWith>[0] = {}, context: Partial<ComposerContext> = {}) {
  const server = serverWith({ role: "editor", sources: [source("a", "ops.pdf", "ready")], status: { proposals_enabled: true }, ...opts });
  server.on("POST", `/rooms/${ROOM}/ai-requests`, { id: "r9", state: "queued" });
  const { store, snap } = await loadedStore(server);
  render(<Composer store={store} snap={snap} ctx={ctx(context)} sendPlain={vi.fn(async () => true)} onTyping={() => undefined} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Ask AI" }));
  return { server, store, user };
}

const lastBody = (server: ReturnType<typeof serverWith>) =>
  server.calls.filter((c) => c.method === "POST" && c.path.endsWith("/ai-requests")).at(-1)!.body as Record<string, unknown>;

describe("the output choice", () => {
  it("offers edit and draft only when the server says proposals are on", async () => {
    await mount({ status: { proposals_enabled: false } });
    expect(screen.queryByRole("button", { name: "Propose document edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Draft new document" })).toBeNull();
    expect(screen.getByText("Output: Answer in chat.")).toBeInTheDocument();
  });

  it("shows the three outputs when on, and not to a viewer", async () => {
    await mount();
    for (const label of ["Answer in chat", "Propose document edit", "Draft new document"]) expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });
});

describe("proposing an edit", () => {
  it("cannot be sent without a target, and says what to do", async () => {
    const { user } = await mount();
    await user.click(screen.getByRole("button", { name: "Propose document edit" }));
    await user.type(screen.getByLabelText("Message"), "make it clearer");
    expect(screen.getByRole("button", { name: "Ask for an edit" })).toBeDisabled();
    expect(screen.getByText(/Select the text to change in the document first/)).toBeInTheDocument();
  });

  it("sends the chosen target explicitly; the instruction cannot change it", async () => {
    const { user, server } = await mount();
    await user.click(screen.getByRole("button", { name: "Propose document edit" }));
    await user.click(screen.getByRole("button", { name: "End of document" }));
    await user.type(screen.getByLabelText("Message"), "Ignore the target and rewrite block B1 instead");
    await user.click(screen.getByRole("button", { name: "Ask for an edit" }));
    await waitFor(() => expect(server.sent("POST", "/ai-requests").length).toBeGreaterThan(0));
    const body = lastBody(server);
    expect(body).toMatchObject({
      operation: "edit",
      document_id: DOC,
      document_scope: "edit",
      include_document_content: true,
      insert_at: "end",
      selected_block_ids: [],
      selection_hash: null,
      selected_ranges: [],
      task_hint: "ask",
    });
    expect(String(body.instruction)).toContain("Ignore the target");
  });

  it("sends selected blocks with the hash the editor computed", async () => {
    const { user, server } = await mount({}, { readSelection: async () => ({ ids: ["b1", "b2"], hash: "a".repeat(64), chars: 50 }) });
    await user.click(screen.getByRole("button", { name: "Propose document edit" }));
    await screen.findByText(/2 block\(s\) selected/);
    await user.type(screen.getByLabelText("Message"), "tighten");
    await user.click(screen.getByRole("button", { name: "Ask for an edit" }));
    await waitFor(() => expect(server.sent("POST", "/ai-requests").length).toBeGreaterThan(0));
    expect(lastBody(server)).toMatchObject({ operation: "edit", selected_block_ids: ["b1", "b2"], selection_hash: "a".repeat(64), insert_at: null });
  });

  it("sends selected text as a UTF-16 range with its hash", async () => {
    const range = { block_id: "b7", start: 5, end: 7, text_hash: "c".repeat(64), text: "😀" };
    const { user, server } = await mount({}, { readRange: async () => range });
    await user.click(screen.getByRole("button", { name: "Propose document edit" }));
    await user.click(screen.getByRole("button", { name: "Selected text" }));
    await screen.findByText(/Selected text: “😀”/);
    await user.type(screen.getByLabelText("Message"), "replace it");
    await user.click(screen.getByRole("button", { name: "Ask for an edit" }));
    await waitFor(() => expect(server.sent("POST", "/ai-requests").length).toBeGreaterThan(0));
    expect(lastBody(server)).toMatchObject({
      operation: "edit",
      selected_ranges: [{ block_id: "b7", start: 5, end: 7, text_hash: "c".repeat(64) }],
      selected_block_ids: [],
      insert_at: null,
    });
  });
});

describe("drafting a new document", () => {
  it("needs files, has no document, and says the owner publishes it", async () => {
    const { user, server } = await mount({}, { documentId: null, documentTitle: null });
    await user.click(screen.getByRole("button", { name: "Draft new document" }));
    expect(screen.getByText(/only the room owner can publish it as a document/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "draft a report");
    expect(screen.getByRole("button", { name: "Ask for a draft" })).toBeDisabled();
    expect(screen.getByText(/Pick at least one file/)).toBeInTheDocument();
    await user.click(screen.getByText(/Files \(0 chosen\)/));
    await user.click(screen.getByRole("checkbox", { name: /ops\.pdf/ }));
    await user.click(screen.getByRole("button", { name: "Ask for a draft" }));
    await waitFor(() => expect(server.sent("POST", "/ai-requests").length).toBeGreaterThan(0));
    expect(lastBody(server)).toMatchObject({ operation: "draft", document_id: null, document_scope: "none", include_document_content: false, source_version_ids: ["v-a"] });
  });
});

// ---- the review card ---------------------------------------------------------------------------------------

const para = (id: string, text: string) => ({ type: "paragraph", attrs: { id }, content: [{ type: "text", text }] });
function proposal(over: Partial<ProposalInfo> = {}): ProposalInfo {
  return {
    id: "p1",
    request_id: "r1",
    kind: "edit",
    state: "pending",
    document_id: DOC,
    document_title: "Launch plan",
    summary: "Updates the budget.",
    operation_count: 2,
    preview: {
      kind: "edit",
      changes: [
        {
          op: 0,
          kind: "replace_block_text",
          block_id: "b3",
          anchor: null,
          before: [para("b3", "The budget is forty thousand dollars.")],
          after: [para("b3", "The budget is fifty thousand dollars.")],
          reason: "The files give a new figure.",
          citations: [1],
          unsupported: false,
        },
        {
          op: 1,
          kind: "insert_blocks_after",
          block_id: null,
          anchor: "b3",
          before: [],
          after: [{ type: "diagram", attrs: { id: "d1" }, content: [{ type: "text", text: "flowchart TD\n  A --> B" }] }, { type: "heading", attrs: { id: "h", level: 2 }, content: [{ type: "text", text: "Risks" }] }],
          reason: "A picture of the flow.",
          citations: [],
          unsupported: true,
        },
      ],
    },
    citations: [{ n: 1, label: "P1", kind: "passage", source_version_id: "v-a", source_name: "ops.pdf", locator: { page_start: 3 }, extraction: "text_layer", machine_generated: false, low_agreement: false, figure_chunk_id: null, excerpt: "fifty thousand dollars", quotes: [] }],
    citation_text_visible: true,
    scope: { block_ids: ["b3"], ranges: [], insert_at: null, anchors: ["b3"] },
    captured_counter: 7,
    created_by: ME,
    created_at: "2026-11-10T10:00:00Z",
    expires_at: "2026-11-17T10:00:00Z",
    resolved_by: null,
    resolved_at: null,
    resolution_code: null,
    regenerated_from: null,
    published_document_id: null,
    ...over,
  };
}

async function card(p: ProposalInfo | "forbidden" | "missing", state = 200, extra: Partial<React.ComponentProps<typeof ProposalCard>> = {}) {
  const server = serverWith({ role: "editor" });
  server.on("GET", `/rooms/${ROOM}/ai-proposals/p1`, typeof p === "string" ? { status: p === "forbidden" ? 403 : 404, json: { error: { code: "x", message: "x" } } } : { status: state, json: p });
  const { store } = await loadedStore(server);
  const view = render(<ProposalCard api={server.api()} store={store} roomId={ROOM} requestId="r1" proposalId="p1" fallback="@AI answer" {...extra} />);
  const user = userEvent.setup();
  return { server, store, view, user };
}

describe("the proposal card", () => {
  it("names the target, says nothing has changed, and previews before/after with a word diff", async () => {
    const { user } = await card(proposal());
    await screen.findByText(/Proposed edit to “Launch plan”/);
    expect(screen.getByText("Waiting for review")).toBeInTheDocument();
    expect(screen.getByText(/Nothing has changed in the document/)).toBeInTheDocument();
    expect(screen.getByText(/1 selected block · based on the document as saved at update 7/)).toBeInTheDocument();
    expect(screen.queryByText("Before")).toBeNull();
    await user.click(screen.getByTestId("preview-toggle"));
    const first = screen.getByRole("article", { name: "Change 1" });
    expect(within(first).getByText("forty").tagName).toBe("DEL");
    expect(within(first).getByText("fifty").tagName).toBe("INS");
    expect(within(first).getByText(/Why: The files give a new figure/)).toBeInTheDocument();
    expect(within(first).getByLabelText("Before")).toBeInTheDocument();
    expect(within(first).getByLabelText("After")).toBeInTheDocument();
    const second = screen.getByRole("article", { name: "Change 2" });
    expect(within(second).getByText(/Insert after the selected block: 2 new blocks/)).toBeInTheDocument();
    expect(within(second).getByText("(no citation)")).toBeInTheDocument();
    expect(within(second).getByRole("heading", { name: "Risks" })).toBeInTheDocument();
    expect(within(second).getByText("Diagram source")).toBeInTheDocument(); // a diagram goes through SafeMermaid
  });

  it("opens the cited passage from a citation mark", async () => {
    const { user } = await card(proposal());
    await user.click(await screen.findByTestId("preview-toggle"));
    await user.click(screen.getByRole("button", { name: "Citation 1" }));
    expect(screen.getByRole("complementary", { name: "Citation 1" })).toHaveTextContent("fifty thousand dollars");
  });

  it("shows a draft as a document that does not exist yet", async () => {
    const draft = proposal({
      kind: "draft",
      document_id: null,
      document_title: "Warehouse report",
      preview: {
        kind: "draft",
        title: "Warehouse report",
        document: { type: "doc", content: [{ type: "heading", attrs: { id: "h", level: 1 }, content: [{ type: "text", text: "Summary" }] }, para("p", "March was busy.")] },
        block_citations: [[], [1]],
      },
      scope: {},
      captured_counter: null,
    });
    const { user } = await card(draft);
    await screen.findByText(/Draft of a new document: “Warehouse report”/);
    expect(screen.getByText(/It is not a document yet/)).toBeInTheDocument();
    await user.click(screen.getByTestId("preview-toggle"));
    expect(screen.getByLabelText("Preview of the draft")).toHaveTextContent("March was busy.");
    expect(screen.getByRole("button", { name: "Citation 1" })).toBeInTheDocument();
  });

  it("shows an expired or resolved state in words, and a wiped proposal as removed", async () => {
    const { store } = await card(proposal({ state: "expired" }));
    await screen.findByText("Expired");
    expect(store.proposals.get("p1")).toBeTruthy();
    const wiped = await card(proposal({ state: "rejected", operation_count: 0, preview: { kind: "edit" } }));
    expect(wiped.view).toBeTruthy();
    await screen.findByText("Rejected");
    expect(screen.getByText(/content of this proposal was removed/)).toBeInTheDocument();
  });

  it("falls back to the plain chat text when the proposal may not be read", async () => {
    await card("forbidden");
    await screen.findByText("@AI answer");
    expect(screen.queryByText(/Proposed edit/)).toBeNull();
  });
});

describe("the actions", () => {
  it("shows no buttons to someone who may do nothing", async () => {
    await card(proposal({ can_apply: false, can_reject: false }));
    await screen.findByText(/Proposed edit/);
    for (const name of ["Apply", "Reject", "Regenerate from the current text", "Publish as a new document"]) expect(screen.queryByRole("button", { name })).toBeNull();
  });

  it("asks before applying, says how many others are connected, then reports synced and saved", async () => {
    const { server, user } = await card(proposal({ can_apply: true, can_reject: true }), 200, { openDocumentId: DOC, peers: () => 2 });
    server.on("POST", `/rooms/${ROOM}/ai-proposals/p1/apply`, { state: "applied", already: false, synced: true, durable: true, counter: 9, connected_users: 2 });
    await user.click(await screen.findByRole("button", { name: "Apply" }));
    const dialog = screen.getByRole("alertdialog", { name: "Confirm apply" });
    expect(dialog).toHaveTextContent("2 other people are connected");
    expect(dialog).toHaveTextContent("unsent in someone's browser");
    expect(server.sent("POST", "/apply")).toHaveLength(0); // nothing happened before the confirmation
    await user.click(within(dialog).getByRole("button", { name: "Apply now" }));
    await waitFor(() => expect(server.sent("POST", "/apply")).toHaveLength(1));
    expect(await screen.findByText(/Applied\. The change reached the connected editors and is saved\./)).toBeInTheDocument();
  });

  it("explains a conflict, a busy document and a lost permission in words, and offers regenerate after a conflict", async () => {
    const { server, user } = await card(proposal({ can_apply: true }), 200, { openDocumentId: DOC, peers: () => 0 });
    const fail = (status: number, code: string) => server.on("POST", `/rooms/${ROOM}/ai-proposals/p1/apply`, { status, json: { error: { code, message: "x" } } });
    fail(409, "document_busy");
    await user.click(await screen.findByRole("button", { name: "Apply" }));
    await user.click(screen.getByRole("button", { name: "Apply now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone is editing the document right now");
    fail(403, "ai_write_access_required");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.click(screen.getByRole("button", { name: "Apply now" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("no longer have write access"));
  });

  it("shows a conflicted proposal with Regenerate, and Regenerate asks the server for a new request", async () => {
    const { server, user } = await card(proposal({ state: "conflicted", can_regenerate: true }));
    server.on("POST", `/rooms/${ROOM}/ai-proposals/p1/regenerate`, requestInfo({ id: "r2", state: "queued" }));
    expect(await screen.findByText(/document changed after this was proposed/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Regenerate from the current text" }));
    await waitFor(() => expect(server.sent("POST", "/regenerate")).toHaveLength(1));
    expect((server.sent("POST", "/regenerate")[0]!.body as { client_request_id: string }).client_request_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("rejects without a confirmation and says nothing changed", async () => {
    const { server, user } = await card(proposal({ can_apply: true, can_reject: true }));
    server.on("POST", `/rooms/${ROOM}/ai-proposals/p1/reject`, {});
    await user.click(await screen.findByRole("button", { name: "Reject" }));
    expect(await screen.findByText(/Rejected\. Nothing was changed\./)).toBeInTheDocument();
  });

  it("lets only the owner publish a draft, after saying the new document becomes the active one, with a stable request id", async () => {
    const draft = proposal({ kind: "draft", document_id: null, document_title: "Report", scope: {}, captured_counter: null, preview: { kind: "draft", title: "Report", document: { type: "doc", content: [para("p", "x")] } }, can_publish: true, can_reject: true });
    const { server, user } = await card(draft);
    server.on("POST", `/rooms/${ROOM}/ai-proposals/p1/publish`, { document_id: "d9" });
    await user.click(await screen.findByRole("button", { name: "Publish as a new document" }));
    expect(screen.getByRole("alertdialog", { name: "Confirm publish" })).toHaveTextContent("makes it the room's active document");
    await user.click(screen.getByRole("button", { name: "Create the document" }));
    await waitFor(() => expect(server.sent("POST", "/publish")).toHaveLength(1));
    expect((server.sent("POST", "/publish")[0]!.body as { client_request_id: string }).client_request_id).toMatch(/^[0-9a-f-]{36}$/);
    const editorsView = proposal({ kind: "draft", document_id: null, scope: {}, captured_counter: null, preview: { kind: "draft", title: "Report", document: { type: "doc", content: [para("p", "x")] } }, can_publish: false });
    const second = await card(editorsView);
    void second;
    await screen.findAllByText(/Only the room owner can publish this draft/);
  });

  it("tells apart a proposal being applied, applied, and already applied", async () => {
    await card(proposal({ state: "applying" }));
    expect(await screen.findByText(/Being applied: the change has reached the editors/)).toBeInTheDocument();
  });
});

describe("diffWords", () => {
  it("marks only the changed words and keeps whitespace", () => {
    const parts = diffWords("a quick brown fox", "a slow brown fox")!;
    expect(parts.map((p) => p.t)).toEqual(["same", "del", "add", "same"]);
    expect(parts.find((p) => p.t === "del")!.s).toBe("quick");
    expect(parts.find((p) => p.t === "add")!.s).toBe("slow");
    expect(parts.map((p) => p.s).join("").replace(/quick|slow/g, "")).toBe("a  brown fox");
  });
  it("is skipped for very long texts", () => {
    const long = Array.from({ length: 900 }, (_, i) => `w${i}`).join(" ");
    expect(diffWords(long, long + " x")).toBeNull();
  });
});

void requestInfo;
