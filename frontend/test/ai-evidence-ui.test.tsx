/** The review-sheet findings that live in the UI: derived text is labelled and leads to the original, assumptions and
 *  context notices are visible, and a rewrite that may have lost a qualification says so before anyone applies it. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AnswerCard } from "../src/ai/Cards";
import { ProposalCard } from "../src/ai/Proposal";
import type { Citation, CitationOrigin, ProposalInfo } from "../src/ai/types";
import { DOC, ROOM, loadedStore, resultOf, serverWith } from "./ai-helpers";

const ORIGINAL = "Build - compile the sources - run the unit tests";
const SUMMARY = "The Build gate compiles the sources and runs the unit tests.";

const derived = (over: Partial<Citation> = {}): Citation => ({
  n: 1,
  label: "S2",
  kind: "section",
  source_version_id: "v-a",
  source_name: "release.md",
  locator: { heading_path: ["Release process", "Build"] },
  extraction: "plain",
  machine_generated: false,
  low_agreement: false,
  figure_chunk_id: null,
  excerpt: SUMMARY,
  quotes: [],
  derived: true,
  derivation: { kind: "section_summary", of: "file", method: "map-v1", prompt: "answer-v2", model: "m" },
  provenance: "complete",
  origin: [{ chunk_id: "c1" }],
  ...over,
});

const origin = (over: Partial<CitationOrigin> = {}): CitationOrigin => ({
  n: 1,
  label: "S2",
  kind: "section",
  derived: true,
  derivation: { kind: "section_summary" },
  provenance: "complete",
  extraction: "plain",
  unavailable: 0,
  origin: [
    {
      kind: "chunk",
      chunk_id: "c1",
      source_id: "s-a",
      source_version_id: "v-a",
      source_name: "release.md",
      file_kind: "md",
      version_no: 1,
      ordinal: 1,
      extraction: "plain",
      page_start: null,
      page_end: null,
      heading_path: ["Release process", "Build"],
      text: ORIGINAL,
      integrity: "verified",
    },
  ],
  ...over,
});

async function open(citation: Citation, reply?: CitationOrigin | { status: number; json: unknown }) {
  const server = serverWith();
  if (reply) server.on("GET", `/rooms/${ROOM}/ai-requests/r1/citations/${citation.n}/origin`, reply);
  const result = resultOf({
    answer: { blocks: [{ type: "paragraph", text: "The build compiles.", citations: [citation.n], unsupported: false }], evidence_gaps: [] },
    citations: [citation],
  });
  render(<AnswerCard api={server.api()} roomId={ROOM} requestId="r1" result={result} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: `Citation ${citation.n}` }));
  return { server, user, detail: screen.getByRole("complementary", { name: `Citation ${citation.n}` }) };
}

describe("a citation of a model-written summary", () => {
  it("is labelled as derived, keeps the summary apart from the original, and shows the original text with its place", async () => {
    const { detail } = await open(derived(), origin());
    expect(within(detail).getByText(/Derived text\./)).toBeInTheDocument();
    expect(within(detail).getByText(/not text of the file/)).toBeInTheDocument();
    const summary = within(detail).getByLabelText("Model-written summary");
    expect(summary).toHaveTextContent(SUMMARY);
    // never offered as a quotation of the file
    expect(within(detail).queryByText(new RegExp(`“${SUMMARY}”`))).toBeNull();
    const list = await within(detail).findByLabelText("Original text");
    expect(list).toHaveTextContent("Original text this summary was made from");
    expect(list).toHaveTextContent(ORIGINAL);
    expect(list).toHaveTextContent(/release\.md \(version 1\) · Release process › Build · part 2 · plain text of the file/);
  });

  it("asks the server for the origin of exactly this citation, and links the original file", async () => {
    const { server, detail } = await open(derived(), origin());
    await within(detail).findByLabelText("Original text");
    expect(server.calls.some((c) => c.method === "GET" && c.path === `/rooms/${ROOM}/ai-requests/r1/citations/1/origin`)).toBe(true);
    expect(within(detail).getByRole("link", { name: /Open the original file/ })).toHaveAttribute("href", `/rooms/${ROOM}/sources/s-a/versions/v-a/download`);
  });

  it("points a PDF page link at the page", async () => {
    const o = origin();
    const chunk = { ...o.origin[0]!, file_kind: "pdf", page_start: 3, page_end: 3, heading_path: [] } as CitationOrigin["origin"][number];
    const { detail } = await open(derived({ locator: { page_start: 3 } }), { ...o, origin: [chunk] });
    expect(await within(detail).findByRole("link", { name: "Open the original file at page 3" })).toHaveAttribute("href", expect.stringMatching(/\/download#page=3$/));
  });

  it("says when the stored text no longer matches what the answer was built on", async () => {
    const o = origin();
    const changed = { ...o.origin[0]!, integrity: "changed" } as CitationOrigin["origin"][number];
    const { detail } = await open(derived(), { ...o, origin: [changed] });
    expect(await within(detail).findByText(/stored text differs from what the answer was built on/)).toBeInTheDocument();
  });

  it("warns when the lineage cannot be traced instead of presenting the reference as sound", async () => {
    const { detail } = await open(derived({ provenance: "incomplete", origin: [] }));
    expect(within(detail).getByText(/original text behind this reference could not be traced/)).toBeInTheDocument();
    expect(within(detail).getByLabelText("Model-written summary")).toBeInTheDocument();
  });

  it("shows the opening of a section as such, not as a summary", async () => {
    const { detail } = await open(derived({ derivation: { kind: "section_opening" } }), origin());
    expect(within(detail).getByText(/opening of a section, kept because the model gave no summary/)).toBeInTheDocument();
  });

  it("keeps a removed source's reference without any text, and does not ask for its origin", async () => {
    const { server, detail } = await open(derived({ source_removed: true, excerpt: undefined }));
    expect(within(detail).getByText(/The source was removed/)).toBeInTheDocument();
    expect(server.calls.some((c) => c.path.endsWith("/origin"))).toBe(false);
  });

  it("does not ask a reader without download rights for the original", async () => {
    const server = serverWith();
    const result = resultOf({
      citation_text_visible: false,
      answer: { blocks: [{ type: "paragraph", text: "x", citations: [1], unsupported: false }], evidence_gaps: [] },
      citations: [derived({ excerpt: undefined })],
    });
    render(<AnswerCard api={server.api()} roomId={ROOM} requestId="r1" result={result} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Citation 1" }));
    expect(screen.getByText(/You may not read the text of this file/)).toBeInTheDocument();
    expect(server.calls.some((c) => c.path.endsWith("/origin"))).toBe(false);
  });

  it("a plain passage is shown as the passage, with its original text, and no derived label", async () => {
    const passage: Citation = { ...derived(), label: "P1", kind: "passage", derived: false, derivation: null, excerpt: "Alice owns Build.", quotes: ["Alice owns Build"] };
    const { detail } = await open(passage, origin({ derived: false, derivation: null, kind: "passage" }));
    await within(detail).findByLabelText("Original text");
    expect(within(detail).queryByText(/Derived text\./)).toBeNull();
    expect(within(detail).getByText(/“Alice owns Build”/)).toBeInTheDocument(); // a validated quotation is still shown as one
  });
});

describe("what an answer admits", () => {
  it("lists the assumptions of a diagram next to it", () => {
    const result = resultOf({
      answer: {
        blocks: [
          {
            type: "diagram",
            title: "Release",
            mermaid: "flowchart TD\n  A[Build] --> B[Deploy]",
            citations: [],
            unsupported: false,
            render_status: "ok",
            render_error: null,
            assumptions: ["Build is drawn before Deploy; the source does not say which comes first."],
          },
        ],
        evidence_gaps: [],
      },
      citations: [],
    });
    render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={result} />);
    const note = screen.getByRole("note", { name: "Assumptions in this diagram" });
    expect(note).toHaveTextContent("Assumed, not stated in the sources:");
    expect(note).toHaveTextContent("the source does not say which comes first");
  });

  it("says when the request's words asked for context that was not switched on", () => {
    const result = resultOf({
      coverage: { notices: [{ code: "document_not_included", message: "You asked about the open document, but it was not included.", action: "Turn on “Use open document” and ask again." }] },
    });
    render(<AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={result} />);
    expect(screen.getByRole("note")).toHaveTextContent(/not included.*Turn on “Use open document”/);
  });

  it("states how much of the document an edit read: all of it, or only part", () => {
    const base = { mode: "target_plus_context", blocks_total: 6, blocks_included: 0, captured_update_counter: 7, scope: "edit" };
    const { unmount } = render(
      <AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={resultOf({ coverage: { document: { ...base, context: { mode: "full", complete: true, blocks_total: 6, blocks_shown: 6, omitted_blocks: 0, clipped_blocks: 0 } } } })} />,
    );
    expect(screen.getByText(/read the rest as context: all 6 blocks/)).toBeInTheDocument();
    unmount();
    render(
      <AnswerCard api={serverWith().api()} roomId={ROOM} requestId="r1" result={resultOf({ coverage: { document: { ...base, mode: "target_plus_partial_context", context: { mode: "sampled", complete: false, blocks_total: 60, blocks_shown: 20, omitted_blocks: 40, clipped_blocks: 12 } } } })} />,
    );
    expect(screen.getByText(/only 20 of 60 blocks \(40 not shown\)/)).toBeInTheDocument();
  });
});

function proposalWith(change: Record<string, unknown>): ProposalInfo {
  const para = (id: string, text: string) => ({ type: "paragraph", attrs: { id }, content: [{ type: "text", text }] });
  return {
    id: "p1",
    request_id: "r1",
    kind: "edit",
    state: "pending",
    document_id: DOC,
    document_title: "Launch plan",
    summary: "Tightens the launch sentence.",
    operation_count: 1,
    preview: {
      kind: "edit",
      changes: [
        {
          op: 0,
          kind: "replace_block_text",
          block_id: "b2",
          anchor: null,
          before: [para("b2", "The launch is scheduled for Friday at noon.")],
          after: [para("b2", "Launch is Friday at noon.")],
          reason: "Shorter.",
          citations: [],
          unsupported: false,
          ...change,
        },
      ],
    },
    citations: [],
    citation_text_visible: true,
    scope: { block_ids: ["b2"] },
    captured_counter: 7,
    created_by: "u1",
    created_at: "2026-11-20T10:00:00Z",
    expires_at: "2026-11-27T10:00:00Z",
    resolved_by: null,
    resolved_at: null,
    resolution_code: null,
    regenerated_from: null,
    published_document_id: null,
    can_apply: true,
    can_reject: true,
  } as unknown as ProposalInfo;
}

async function card(p: ProposalInfo) {
  const server = serverWith({ role: "editor" });
  server.on("GET", `/rooms/${ROOM}/ai-proposals/p1`, p);
  const { store } = await loadedStore(server);
  render(<ProposalCard api={server.api()} store={store} roomId={ROOM} requestId="r1" proposalId="p1" fallback="@AI answer" />);
  const user = userEvent.setup();
  await user.click(await screen.findByTestId("preview-toggle"));
  return user;
}

describe("a rewrite that may have lost a qualification", () => {
  it("tells the reviewer what to check before applying, and says the check is only a word-level one", async () => {
    await card(proposalWith({ review_flags: [{ code: "status_changed", detail: "“scheduled” (planned) is gone: the text no longer says the work is planned" }] }));
    const flags = await screen.findByRole("note", { name: "Check these before applying" });
    expect(flags).toHaveTextContent(/new text may say something different/);
    expect(flags).toHaveTextContent(/“scheduled” \(planned\) is gone/);
    expect(flags).toHaveTextContent(/can miss a change and it can flag a harmless one/);
  });

  it("shows nothing when the check found nothing", async () => {
    await card(proposalWith({ review_flags: [] }));
    await waitFor(() => expect(screen.getByRole("article", { name: "Change 1" })).toBeInTheDocument());
    expect(screen.queryByRole("note", { name: "Check these before applying" })).toBeNull();
  });

  it("lists the assumptions of a diagram the proposal inserts", async () => {
    await card(proposalWith({ assumptions: ["Build is drawn before Deploy."] }));
    expect(await screen.findByRole("note", { name: "Assumptions in this diagram" })).toHaveTextContent("Build is drawn before Deploy.");
  });
});
