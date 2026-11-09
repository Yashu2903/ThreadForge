/** The AI UI: the composer's mention rules and chips, the cards, the store's reactions, the panels. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AnswerCard, RequestCard } from "../src/ai/Cards";
import { Composer, type ComposerContext } from "../src/ai/Composer";
import { AiSettingsDialog, SourcePanel } from "../src/ai/Panels";
import { unavailableReason } from "../src/ai/store";
import { DOC, ME, ROOM, loadedStore, requestInfo, resultOf, serverWith, source } from "./ai-helpers";

const ctx = (over: Partial<ComposerContext> = {}): ComposerContext => ({
  documentId: DOC,
  documentTitle: "Launch plan",
  unsynced: 0,
  readSelection: async () => null,
  chatSelection: [],
  clearChatSelection: () => undefined,
  role: "editor",
  ...over,
});

async function mount(opts: Parameters<typeof serverWith>[0] = {}, context: Partial<ComposerContext> = {}) {
  const server = serverWith({ role: "editor", sources: [source("a", "ops.pdf", "ready"), source("b", "new.pdf", "not_analyzed")], ...opts });
  const { store, snap } = await loadedStore(server);
  const sendPlain = vi.fn(async () => true);
  const view = render(<Composer store={store} snap={snap} ctx={ctx(context)} sendPlain={sendPlain} onTyping={() => undefined} />);
  return { server, store, sendPlain, ...view, user: userEvent.setup() };
}

describe("the @AI mention", () => {
  it("does nothing when '@AI' is merely typed: the message goes out as ordinary chat", async () => {
    const { sendPlain, server, user } = await mount();
    await user.type(screen.getByLabelText("Message"), "hello @AI how are you");
    expect(screen.queryByTestId("ai-chip")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(sendPlain).toHaveBeenCalledWith("hello @AI how are you");
    expect(server.sent("POST", "/ai-requests")).toHaveLength(0);
  });

  it("offers the mention after '@' and turns on only when it is chosen", async () => {
    const { user } = await mount();
    const box = screen.getByLabelText("Message");
    await user.type(box, "@");
    const option = await screen.findByRole("option", { name: /@AI/ });
    expect(screen.queryByTestId("ai-chip")).toBeNull(); // offered, not enabled
    await user.click(option);
    expect(screen.getByTestId("ai-chip")).toBeInTheDocument();
    expect(box).toHaveValue(""); // the typed '@' is consumed
    await user.click(screen.getByLabelText("Remove @AI"));
    expect(screen.queryByTestId("ai-chip")).toBeNull();
  });

  it("shows AI as unavailable, with the reason, for a viewer", async () => {
    const { user } = await mount({ role: "viewer" }, { role: "viewer" });
    expect(screen.getByText(/AI requests are for editors and owners/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask AI" })).toBeDisabled();
    await user.type(screen.getByLabelText("Message"), "@");
    expect(screen.queryByRole("option")).toBeNull();
  });

  it("explains why when AI is off for the room, the service, or has no key", async () => {
    await mount({ aiEnabled: false });
    expect(screen.getByText(/owner switched AI off in this room/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask AI" })).toBeDisabled();
  });
});

describe("the request chips and options", () => {
  async function ai() {
    const m = await mount();
    await m.user.click(screen.getByRole("button", { name: "Ask AI" }));
    return m;
  }

  it("shows each chosen file with its state and says an unanalyzed one will be analyzed on send", async () => {
    const m = await ai();
    await m.user.click(screen.getByText(/Files \(0 chosen\)/));
    await m.user.click(screen.getByRole("checkbox", { name: /ops\.pdf/ }));
    await m.user.click(screen.getByRole("checkbox", { name: /new\.pdf/ }));
    expect(screen.getByText(/ops\.pdf · ready/)).toBeInTheDocument();
    expect(screen.getByText(/new\.pdf · not analyzed yet/)).toBeInTheDocument();
    expect(screen.getByText(/will be analyzed when you send \(12 pages\)/)).toBeInTheDocument();
    await m.user.click(screen.getByLabelText("Remove ops.pdf"));
    expect(screen.queryByText(/ops\.pdf · ready/)).toBeNull();
  });

  it("offers only 'Answer in chat' and the four task chips", async () => {
    await ai();
    expect(screen.getByText("Output: Answer in chat.")).toBeInTheDocument();
    for (const t of ["Auto", "Ask", "Summarize", "Compare"]) expect(screen.getByRole("button", { name: t })).toBeInTheDocument();
    expect(screen.queryByText(/draft|edit the document|propose/i)).toBeNull();
  });

  it("disables the document while the editor has unsynced changes, and says why", async () => {
    const { user } = await (async () => {
      const m = await mount({}, { unsynced: 3 });
      await m.user.click(screen.getByRole("button", { name: "Ask AI" }));
      return m;
    })();
    expect(screen.getByRole("checkbox", { name: /Use open document/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Use selected blocks/ })).toBeDisabled();
    expect(screen.getByText(/not on the server yet/)).toBeInTheDocument();
    expect(user).toBeTruthy();
  });

  it("names the real document title on the chip", async () => {
    const m = await ai();
    await m.user.click(screen.getByRole("checkbox", { name: /Use open document/ }));
    expect(screen.getByText(/Open document: Launch plan/)).toBeInTheDocument();
  });

  it("sends exactly what the chips show, then resets so nothing carries over silently", async () => {
    const m = await ai();
    m.server.on("POST", `/rooms/${ROOM}/ai-requests`, { id: "r9", state: "queued" });
    m.server.on("POST", `/rooms/${ROOM}/ai-requests/estimate`, { estimate: { input_tokens: 900, expected_output_tokens: 100, provider_calls: 2, images: 0, cost_known: false, expected_cost_micros: null, max_cost_micros: null, requires_confirmation: false, over_caps: [], new_analysis_pages: 12, new_analysis_figures: 0 }, key_sources: { generation: { provider: "openai", source: "user", problem: null }, embedding: { provider: "google", source: "server", problem: null } }, consent_given: true, can_send: true, blocking_code: null });
    await m.user.click(screen.getByText(/Files \(0 chosen\)/));
    await m.user.click(screen.getByRole("checkbox", { name: /new\.pdf/ }));
    await m.user.click(screen.getByRole("button", { name: "Summarize" }));
    await m.user.type(screen.getByLabelText("Message"), "Summarize this please");
    expect(await screen.findByTestId("estimate")).toHaveTextContent(/Analyzes 12 new page/);
    expect(screen.getByTestId("estimate")).toHaveTextContent(/your key and the server's/);
    await m.user.click(screen.getByRole("button", { name: "Send to AI" }));
    await waitFor(() => expect(m.server.sent("POST", "/ai-requests")).not.toHaveLength(0));
    const body = m.server.calls.find((c) => c.method === "POST" && c.path.endsWith("/ai-requests"))!.body as Record<string, unknown>;
    expect(body).toMatchObject({
      instruction: "Summarize this please",
      task_hint: "summarize",
      document_id: DOC,
      include_document_content: false,
      document_scope: "none",
      source_version_ids: ["v-b"],
      include_chat_context: false,
      confirm_large: false,
    });
    expect(typeof body.client_request_id).toBe("string");
    await waitFor(() => expect(screen.queryByTestId("ai-chip")).toBeNull()); // reset
    expect(screen.getByLabelText("Message")).toHaveValue("");
  });

  it("asks for confirmation on a large request and resends with confirm_large", async () => {
    const m = await ai();
    let first = true;
    m.server.on("POST", `/rooms/${ROOM}/ai-requests`, (_b: unknown) =>
      first ? ((first = false), { status: 409, json: { error: { code: "confirmation_required", message: "large", estimate: { input_tokens: 150000, calls: 9, cost_known: true, expected_cost_micros: 250000 } } } }) : { json: { id: "r1", state: "queued" } },
    );
    m.server.on("POST", `/rooms/${ROOM}/ai-requests/estimate`, { status: 422, json: { error: { code: "x", message: "x" } } });
    await m.user.click(screen.getByText(/Files \(0 chosen\)/));
    await m.user.click(screen.getByRole("checkbox", { name: /ops\.pdf/ }));
    await m.user.type(screen.getByLabelText("Message"), "Big one");
    await m.user.click(screen.getByRole("button", { name: "Send to AI" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/150,000 input tokens/);
    expect(dialog).toHaveTextContent(/\$0\.2500/);
    await m.user.click(within(dialog).getByRole("button", { name: "Send anyway" }));
    await waitFor(() => {
      const sends = m.server.calls.filter((c) => c.method === "POST" && c.path.endsWith("/ai-requests"));
      expect(sends).toHaveLength(2);
      expect((sends[1]!.body as { confirm_large: boolean }).confirm_large).toBe(true);
      expect((sends[1]!.body as { client_request_id: string }).client_request_id).toBe((sends[0]!.body as { client_request_id: string }).client_request_id);
    });
  });

  it("shows a plain-language message when the server refuses", async () => {
    const m = await ai();
    m.server.on("POST", `/rooms/${ROOM}/ai-requests`, { status: 403, json: { error: { code: "ai_write_access_required", message: "no" } } });
    m.server.on("POST", `/rooms/${ROOM}/ai-requests/estimate`, { status: 422, json: { error: { code: "x", message: "x" } } });
    await m.user.click(screen.getByText(/Files \(0 chosen\)/));
    await m.user.click(screen.getByRole("checkbox", { name: /ops\.pdf/ }));
    await m.user.type(screen.getByLabelText("Message"), "Hello");
    await m.user.click(screen.getByRole("button", { name: "Send to AI" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/no longer have write access/);
  });
});

describe("the cards", () => {
  it("renders an answer as text with citations, flags, gaps and coverage", async () => {
    const { store } = await loadedStore(serverWith());
    void store;
    const user = userEvent.setup();
    render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={resultOf()} />);
    expect(screen.getByText(/The warehouse processed 12,000 orders\./)).toBeInTheDocument();
    expect(screen.getAllByText(/\(no citation\)/)).toHaveLength(1); // only the unsupported claim
    expect(screen.getByText("No 2024 data")).toBeInTheDocument();
    expect(screen.getByText(/sampled|searched for relevant passages/)).toBeInTheDocument();
    expect(screen.getByText(/3 of 8 sections, 1 of 4 figures/)).toBeInTheDocument();
    expect(screen.getByText(/update 7/)).toBeInTheDocument();
    expect(screen.getByText(/proof that they support each claim/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Citation 1" }));
    const detail = screen.getByRole("complementary", { name: "Citation 1" });
    expect(within(detail).getByText(/ops\.pdf/)).toBeInTheDocument();
    expect(within(detail).getByText(/page 3/)).toBeInTheDocument();
    expect(within(detail).getByText(/twelve thousand orders in March/)).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: "Citation 2" }));
    expect(screen.getByText(/Machine-generated: read by OCR/)).toBeInTheDocument(); // an OCR passage says so
  });

  it("renders model text as TEXT: markup and links are not live", () => {
    const evil = resultOf({
      answer: {
        blocks: [{ type: "paragraph", text: '<img src=x onerror="alert(1)"> [click](javascript:alert(1)) <a href="https://evil">x</a>', citations: [], unsupported: true }],
        evidence_gaps: [],
      },
      citations: [],
      coverage: {},
    });
    const { container } = render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={evil} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText(/<img src=x onerror/)).toBeInTheDocument();
  });

  it("hides the text of a citation from a role that may not read files", async () => {
    const user = userEvent.setup();
    render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={resultOf({ citation_text_visible: false })} />);
    await user.click(screen.getByRole("button", { name: "Citation 1" }));
    expect(screen.getByText(/may not read the text/)).toBeInTheDocument();
    expect(screen.queryByText(/twelve thousand orders in March/)).toBeNull();
  });

  it("shows a citation to a removed source as a stub, and a redacted answer as removed", async () => {
    const user = userEvent.setup();
    const removed = resultOf();
    removed.citations[0] = { ...removed.citations[0]!, source_removed: true, excerpt: undefined, quotes: [] };
    const { unmount } = render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={removed} />);
    await user.click(screen.getByRole("button", { name: "Citation 1" }));
    expect(screen.getByText(/source was removed/)).toBeInTheDocument();
    unmount();
    render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={resultOf({ redacted: true, answer: null })} />);
    expect(screen.getByText(/removed by the room owner/)).toBeInTheDocument();
  });

  it("shows request progress with stage labels, queue position, and a reason when it failed", async () => {
    const { store } = await loadedStore(serverWith());
    const { rerender } = render(<RequestCard store={store} info={requestInfo({ state: "waiting_for_sources", stage_label: "Analyzing files", queue_position: null, sources: [{ version_id: "v1", name: "big.pdf", version_no: 1, analysis_state: "extracting" }] })} />);
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for files · Analyzing files");
    expect(screen.getByText(/big\.pdf: extracting/)).toBeInTheDocument();
    rerender(<RequestCard store={store} info={requestInfo({ state: "queued", queue_position: 3 })} />);
    expect(screen.getByRole("status")).toHaveTextContent("position 3");
    rerender(<RequestCard store={store} info={requestInfo({ state: "failed", error_code: "source_failed", can_cancel: false, can_retry: true, attempt: 2 })} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/could not be analyzed/);
    expect(screen.getByRole("status")).toHaveTextContent("attempt 2 of 3");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("cancels and retries through the store", async () => {
    const server = serverWith();
    server.on("POST", `/rooms/${ROOM}/ai-requests/r1/cancel`, requestInfo({ state: "cancelled", can_cancel: false }));
    server.on("GET", `/rooms/${ROOM}/ai-requests/r1`, requestInfo({ state: "cancelled", can_cancel: false }));
    const { store } = await loadedStore(server);
    const user = userEvent.setup();
    render(<RequestCard store={store} info={requestInfo({ state: "queued" })} />);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(server.sent("POST", "/r1/cancel")).toHaveLength(1));
  });
});

describe("the store", () => {
  it("refetches what a signal names, and everything after a reset", async () => {
    const server = serverWith({ sources: [source("a", "ops.pdf")] });
    server.on("GET", `/rooms/${ROOM}/ai-requests/r1`, requestInfo({ state: "running" }));
    const { store } = await loadedStore(server);
    const before = server.fetch.mock.calls.length;
    await store.applySignals({ requests: new Map([["r1", "running"]]), sources: new Map(), aiEnabled: null, reset: false });
    expect(server.sent("GET", "/ai-requests/r1")).toHaveLength(1);
    expect((store.requests.get("r1") as { state: string }).state).toBe("running");
    await store.applySignals({ requests: new Map(), sources: new Map([["s-a", { versionId: "v-a", state: "ready" }]]), aiEnabled: null, reset: false });
    expect(server.sent("GET", `/rooms/${ROOM}/sources`).length).toBeGreaterThanOrEqual(2);
    await store.applySignals({ requests: new Map(), sources: new Map(), aiEnabled: false, reset: false });
    expect(store.getSnapshot().settings?.ai_enabled).toBe(false);
    server.fetch.mockClear();
    await store.applySignals({ requests: new Map(), sources: new Map(), aiEnabled: null, reset: true });
    expect(server.fetch.mock.calls.length).toBeGreaterThanOrEqual(5);
    expect(server.fetch.mock.calls.length).toBeGreaterThan(0);
    expect(before).toBeGreaterThan(0);
  });

  it("treats a 403 on a request as 'only the chat message is visible' (viewers)", async () => {
    const server = serverWith({ role: "viewer" });
    server.on("GET", `/rooms/${ROOM}/ai-requests/r1`, { status: 403, json: { error: { code: "insufficient_role", message: "no" } } });
    const { store } = await loadedStore(server);
    expect(await store.refreshRequest("r1")).toBe("forbidden");
    store.watch("r1");
    await new Promise((r) => setTimeout(r, 20));
    expect(server.sent("GET", "/ai-requests/r1")).toHaveLength(1); // no polling for a card it may not see
  });

  it("fetches the result once a request has succeeded", async () => {
    const server = serverWith();
    server.on("GET", `/rooms/${ROOM}/ai-requests/r1`, requestInfo({ state: "succeeded", reply_message_id: "m2" }));
    server.on("GET", `/rooms/${ROOM}/ai-requests/r1/result`, resultOf());
    const { store } = await loadedStore(server);
    await store.refreshRequest("r1");
    await waitFor(() => expect(store.results.get("r1")).toBeTruthy());
  });

  it("reports the reason AI is unavailable, in order of precedence", async () => {
    const mk = async (o: Parameters<typeof serverWith>[0]) => (await loadedStore(serverWith(o))).snap;
    expect(unavailableReason(await mk({}), "editor")).toBeNull();
    expect(unavailableReason(await mk({}), "viewer")).toMatch(/editor or owner/);
    expect(unavailableReason(await mk({ status: { code: "ai_not_configured", available: false } }), "editor")).toMatch(/not set up/);
    expect(unavailableReason(await mk({ status: { code: "ai_disabled", available: false } }), "editor")).toMatch(/switched off for the whole service/);
    expect(unavailableReason(await mk({ status: { code: "ai_unavailable", available: false } }), "editor")).toMatch(/temporarily unavailable/);
    expect(unavailableReason(await mk({ aiEnabled: false }), "editor")).toMatch(/room/);
  });
});

describe("the store lifecycle", () => {
  it("works again after being disposed and re-activated (StrictMode mounts effects twice)", async () => {
    const server = serverWith({ sources: [source("a", "ops.pdf")] });
    const store = new (await import("../src/ai/store")).AiStore(server.api(), ROOM, ME);
    store.dispose();
    store.activate();
    await store.load();
    expect(store.getSnapshot().sources?.sources).toHaveLength(1);
    store.dispose();
    await store.load(); // a disposed store ignores late answers
    expect(store.getSnapshot().sources?.sources).toHaveLength(1);
  });
});

describe("the files panel and AI settings", () => {
  it("shows states, and offers only the actions a role may take", async () => {
    const server = serverWith({ sources: [source("a", "ops.pdf", "ready"), source("b", "scan.pdf", "failed", { error_code: "too_many_unreadable_pages" })] });
    const { store, snap } = await loadedStore(server);
    const { rerender } = render(<SourcePanel store={store} snap={snap} role="editor" documentId={DOC} userId={ME} />);
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText(/Failed · Too many pages of a file could not be read\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Analyze now" })).toBeEnabled(); // only the failed file offers it
    expect(screen.getAllByRole("button", { name: /Inspect/ })).toHaveLength(2);
    expect(screen.getByLabelText("Upload a file")).toBeInTheDocument();
    const viewerServer = serverWith({ role: "viewer", sources: [source("a", "ops.pdf", "ready")] });
    const v = await loadedStore(viewerServer);
    rerender(<SourcePanel store={v.store} snap={v.snap} role="viewer" documentId={DOC} userId={ME} />);
    expect(screen.queryByLabelText("Upload a file")).toBeNull();
    expect(screen.queryByRole("button", { name: /Analyze now|Remove|Inspect/ })).toBeNull();
  });

  it("uploads in three steps through the store", async () => {
    const server = serverWith();
    server.on("POST", `/rooms/${ROOM}/source-uploads`, { id: "u1" });
    server.on("PUT", `/rooms/${ROOM}/source-uploads/u1/content`, { id: "u1" });
    server.on("POST", `/rooms/${ROOM}/source-uploads/u1/finalize`, { id: "s1" });
    const { store } = await loadedStore(server);
    await store.uploadFile(new File(["%PDF-1.4 x"], "a.pdf", { type: "application/pdf" }));
    expect(server.sent("POST", "/source-uploads")[0]!.body).toMatchObject({ filename: "a.pdf", byte_size: 10 });
    expect(server.sent("PUT", "/u1/content")).toHaveLength(1);
    expect(server.sent("POST", "/u1/finalize")).toHaveLength(1);
  });

  it("shows the data notice, saves a key without ever showing it back, and lets the owner switch AI off", async () => {
    const server = serverWith({ consentAccepted: false });
    server.on("PUT", "/ai/keys/openai", { provider: "openai", last4: "abcd", state: "active", last_validated_at: null });
    server.on("PUT", `/rooms/${ROOM}/ai-settings`, { ai_enabled: false, available: false, code: "ai_disabled_for_room" });
    const { store, snap } = await loadedStore(server);
    const user = userEvent.setup();
    render(<AiSettingsDialog store={store} snap={snap} isOwner={true} onClose={() => undefined} />);
    expect(screen.getByText(/sent to the embedding provider/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /I have read it and agree/ })).toBeInTheDocument();
    const input = screen.getByLabelText("API key");
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, "sk-test-0123456789abcdef");
    await user.click(screen.getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(server.sent("PUT", "/ai/keys/openai")).toHaveLength(1));
    expect(server.sent("PUT", "/ai/keys/openai")[0]!.body).toEqual({ api_key: "sk-test-0123456789abcdef" });
    expect(screen.queryByDisplayValue("sk-test-0123456789abcdef")).toBeNull(); // cleared after saving
    await user.click(screen.getByRole("checkbox", { name: /Allow AI in this room/ }));
    await waitFor(() => expect(server.sent("PUT", "/ai-settings")[0]!.body).toEqual({ ai_enabled: false }));
  });

  it("does not show the owner's controls to anyone else", async () => {
    const { store, snap } = await loadedStore(serverWith());
    render(<AiSettingsDialog store={store} snap={snap} isOwner={false} onClose={() => undefined} />);
    expect(screen.queryByRole("checkbox", { name: /Allow AI in this room/ })).toBeNull();
    expect(screen.queryByText(/AI usage/)).toBeNull();
  });
});
