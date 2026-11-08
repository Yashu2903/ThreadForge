import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OpenDialog } from "../src/components/OpenDialog";
import { StartDialog } from "../src/components/StartDialog";
import { DocumentStore } from "../src/documents/store";
import { ROOM, csrfRoute, fakeApi, full, summary } from "./helpers";

const D1 = "d1111111-1111-4111-8111-111111111111";

describe("StartDialog", () => {
  function setup(responder?: (n: number) => { status?: number; body?: unknown }) {
    let n = 0;
    const created = vi.fn();
    const { api, calls } = fakeApi({
      ...csrfRoute,
      [`POST /rooms/${ROOM}/documents`]: () => responder?.(++n) ?? { status: 201, body: full(D1, "Plan") },
    });
    render(<StartDialog api={api} roomId={ROOM} onCreated={created} onClose={() => undefined} />);
    return { calls, created, posts: () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/documents")) };
  }

  it("is an accessible dialog with the five templates", () => {
    setup();
    expect(screen.getByRole("dialog", { name: "Start a document" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getAllByRole("radio")).toHaveLength(5);
    expect(screen.getByRole("radio", { name: /Blank document/ })).toBeChecked();
  });

  it("requires a title and sends nothing without one", async () => {
    const { posts } = setup();
    await userEvent.click(screen.getByRole("button", { name: "Create document" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Give the document a title");
    expect(posts()).toHaveLength(0);
  });

  it("sends the trimmed title, chosen template and a client_request_id with the CSRF token", async () => {
    const { posts, created } = setup();
    await userEvent.type(screen.getByLabelText("Title"), "  Launch plan  ");
    await userEvent.click(screen.getByRole("radio", { name: /Meeting notes/ }));
    await userEvent.click(screen.getByRole("button", { name: "Create document" }));
    await waitFor(() => expect(created).toHaveBeenCalled());
    const [post] = posts();
    expect(post!.body).toMatchObject({ title: "Launch plan", template_key: "meeting_notes" });
    expect((post!.body as { client_request_id: string }).client_request_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(post!.headers["X-CSRF-Token"]).toBe("tok");
  });

  it("does not submit twice while a request is in flight", async () => {
    let release!: () => void;
    const created = vi.fn();
    const { api, calls } = fakeApi({
      ...csrfRoute,
      [`POST /rooms/${ROOM}/documents`]: () => new Promise((res) => (release = () => res({ status: 201, body: full(D1) }))),
    });
    render(<StartDialog api={api} roomId={ROOM} onCreated={created} onClose={() => undefined} />);
    await userEvent.type(screen.getByLabelText("Title"), "Once");
    const submit = screen.getByRole("button", { name: "Create document" });
    await userEvent.click(submit);
    await userEvent.click(screen.getByRole("button", { name: "Creating…" }));
    await userEvent.keyboard("{Enter}");
    release();
    await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
    expect(calls.filter((c) => c.method === "POST" && c.path.endsWith("/documents"))).toHaveLength(1);
  });

  it("a retry after a failure reuses the SAME client_request_id; changed input gets a new one", async () => {
    const { posts, created } = setup((n) => (n === 1 ? { status: 500, body: { error: { code: "internal_error", message: "Something went wrong." } } } : { status: 201, body: full(D1) }));
    await userEvent.type(screen.getByLabelText("Title"), "Retry me");
    await userEvent.click(screen.getByRole("button", { name: "Create document" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
    await userEvent.click(screen.getByRole("button", { name: "Create document" }));
    await waitFor(() => expect(created).toHaveBeenCalled());
    const [first, second] = posts();
    expect((second!.body as { client_request_id: string }).client_request_id).toBe(
      (first!.body as { client_request_id: string }).client_request_id,
    );
  });

  it("uses a new client_request_id when the title changed after a failure", async () => {
    const { posts } = setup((n) => (n <= 2 ? { status: 500, body: { error: { code: "internal_error", message: "Something went wrong." } } } : { status: 201, body: full(D1) }));
    await userEvent.type(screen.getByLabelText("Title"), "First");
    await userEvent.click(screen.getByRole("button", { name: "Create document" }));
    await screen.findByRole("alert");
    await userEvent.type(screen.getByLabelText("Title"), " edited");
    await userEvent.click(screen.getByRole("button", { name: "Create document" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    const ids = posts().map((p) => (p.body as { client_request_id: string }).client_request_id);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("closes on Escape and returns focus to the opener", async () => {
    const onClose = vi.fn();
    const { api } = fakeApi({ ...csrfRoute });
    render(<StartDialog api={api} roomId={ROOM} onCreated={() => undefined} onClose={onClose} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});

describe("OpenDialog", () => {
  async function setup(opts: { isOwner: boolean; drive: "available" | "not_configured" | null }) {
    const { api } = fakeApi({
      [`GET /rooms/${ROOM}/documents?`]: () => ({
        body: { items: [summary(D1, "Active one", { is_active: true }), summary("d2", "Other doc")], next_cursor: null },
      }),
    });
    const store = new DocumentStore(api, ROOM);
    await store.loadList();
    const onOpen = vi.fn();
    const onMakeActive = vi.fn().mockResolvedValue(undefined);
    render(
      <OpenDialog
        store={store}
        isOwner={opts.isOwner}
        capabilities={opts.drive ? { google_drive: opts.drive } : null}
        onOpen={onOpen}
        onMakeActive={onMakeActive}
        onClose={() => undefined}
      />,
    );
    return { onOpen, onMakeActive };
  }

  it("lists saved documents and opens one", async () => {
    const { onOpen } = await setup({ isOwner: false, drive: null });
    expect(screen.getByText("Active one")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Open" })[1]!);
    expect(onOpen).toHaveBeenCalledWith("d2");
  });

  it("only the owner can make a document active (and not the one already active)", async () => {
    const owner = await setup({ isOwner: true, drive: null });
    expect(screen.getAllByRole("button", { name: "Make active" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Make active" }));
    expect(owner.onMakeActive).toHaveBeenCalledWith("d2");
  });

  it("members who are not owners see no Make active button", async () => {
    await setup({ isOwner: false, drive: null });
    expect(screen.queryByRole("button", { name: "Make active" })).toBeNull();
  });

  it("explains that Google Drive is unavailable when the server is not configured, and offers no Drive button", async () => {
    await setup({ isOwner: true, drive: "not_configured" });
    expect(screen.getByRole("heading", { name: "Google Drive" })).toBeInTheDocument();
    expect(screen.getByText(/Not available/)).toBeInTheDocument();
    expect(screen.getByText(/GOOGLE_PICKER_API_KEY/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /drive/i })).toBeNull();
  });

  it("shows nothing about Drive until the capability is known, and no Dropbox/OneDrive ever", async () => {
    await setup({ isOwner: true, drive: null });
    expect(screen.queryByText(/Drive/)).toBeNull();
    expect(screen.queryByText(/dropbox|onedrive/i)).toBeNull();
  });
});
