import { type Page, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * AI phase 2 in two real browsers, against the real API, the real AI worker, the real collaboration service and
 * PostgreSQL. Only the two AI providers are a local stand-in (backend/scripts/e2e_ai_stub.py), so this proves
 * the WIRING (target choice, review card, apply protocol, convergence, conflicts, undo, draft publishing); it
 * proves nothing about how a real model writes.
 */
const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");
const NOTES = "# Operations\n\nThe warehouse processed twelve thousand orders in March.\n";

async function setup(browser: import("@playwright/test").Browser) {
  const owner = seed("Olivia Owner");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Proposal room");
  const doc = await asOwner.createDocument(roomId, "Launch plan");
  await asOwner.send("POST", "/ai/consent", { notice_version: 1 });
  const editor = seed("Eddie Editor", { roomId, role: "editor" });
  const a = await signedIn(browser, owner);
  const b = await signedIn(browser, editor);
  const asEditor = await AsUser.create(editor);
  await asEditor.send("POST", "/ai/consent", { notice_version: 1 });
  for (const p of [a.page, b.page]) {
    await p.goto(roomUrl(roomId));
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText("Synced");
  }
  return { roomId, doc, asOwner, a, b };
}

async function ask(page: Page, output: string, target: string | null, text: string) {
  await page.getByRole("button", { name: "Ask AI" }).click();
  await page.getByRole("button", { name: output, exact: true }).click();
  if (target) await page.getByRole("button", { name: target, exact: true }).click();
  await page.getByRole("textbox", { name: "Message" }).fill(text);
}

test.describe("AI proposals in two browsers", () => {
  test("preview, apply an insertion and a replacement; both editors converge; undo does not undo the AI", async ({ browser }) => {
    test.setTimeout(150_000);
    const { a, b, asOwner, roomId } = await setup(browser);
    await surface(a.page).click();
    await a.page.keyboard.type("The launch is on Friday.");
    await expect(surface(b.page)).toContainText("The launch is on Friday.");
    await expect(status(a.page)).toHaveText("Synced");

    // 1. an insertion at the end of the document
    await ask(a.page, "Propose document edit", "End of document", "Add a requirements section.");
    await a.page.getByRole("button", { name: "Ask for an edit" }).click();
    const card = a.page.getByRole("region", { name: "AI proposal" });
    await expect(card).toContainText("Proposed edit to “Launch plan”", { timeout: 60_000 });
    await expect(card).toContainText("Nothing has changed in the document");
    await expect(surface(b.page)).not.toContainText("A requirements section.");
    await card.getByTestId("preview-toggle").click();
    await expect(card).toContainText("A requirements section.");
    await card.getByRole("button", { name: "Apply" }).click();
    await expect(a.page.getByRole("alertdialog", { name: "Confirm apply" })).toContainText("1 other person is connected");
    await a.page.getByRole("button", { name: "Apply now" }).click();
    await expect(card).toContainText("Applied", { timeout: 30_000 });
    await expect(surface(a.page)).toContainText("A requirements section.");
    await expect(surface(b.page)).toContainText("A requirements section.");
    await expect(status(b.page)).toHaveText("Synced");

    // 2. the second editor types; Ctrl+Z only undoes their own typing, never the AI's insertion
    await surface(b.page).click();
    await b.page.keyboard.press("Control+End");
    await b.page.keyboard.type(" Eddie wrote this.");
    await expect(surface(a.page)).toContainText("Eddie wrote this.");
    await b.page.keyboard.press("Control+z");
    await expect(surface(b.page)).not.toContainText("Eddie wrote this.");
    await expect(surface(b.page)).toContainText("A requirements section.");

    // 3. applying again changes nothing (the card is final); the stored document has the insertion
    await expect(card.getByRole("button", { name: "Apply" })).toHaveCount(0);
    const stored = await asOwner.get<{ items: { id: string }[] }>(`/rooms/${roomId}/documents`);
    expect(stored.items.length).toBe(1);
  });

  test("a changed target gives a conflict that changes nothing; Regenerate asks again from the new text", async ({ browser }) => {
    test.setTimeout(150_000);
    const { a, b } = await setup(browser);
    await surface(a.page).click();
    await a.page.keyboard.type("Budget is forty thousand dollars.");
    await expect(surface(b.page)).toContainText("Budget is forty thousand");
    await expect(status(a.page)).toHaveText("Synced");

    // select the paragraph's text and ask for a rewrite of the selected block
    await surface(a.page).click({ clickCount: 3 });
    await ask(a.page, "Propose document edit", "Selected blocks", "Make this clearer.");
    await a.page.getByRole("button", { name: "Ask for an edit" }).click();
    const card = a.page.getByRole("region", { name: "AI proposal" });
    await expect(card).toContainText("Proposed edit", { timeout: 60_000 });

    // the other browser edits the targeted block before Apply
    await surface(b.page).click();
    await b.page.keyboard.press("Control+End");
    await b.page.keyboard.type(" (Eddie changed this)");
    await expect(surface(a.page)).toContainText("Eddie changed this");

    await card.getByRole("button", { name: "Apply" }).click();
    await a.page.getByRole("button", { name: "Apply now" }).click();
    await expect(card).toContainText("The document changed after this was proposed", { timeout: 30_000 });
    await expect(surface(a.page)).toContainText("Budget is forty thousand dollars. (Eddie changed this)");
    await expect(surface(a.page)).not.toContainText("Rewritten by the assistant.");

    // Regenerate: a new request from the current text; the old card stays readable
    await card.getByRole("button", { name: "Regenerate from the current text" }).click();
    const cards = a.page.getByRole("region", { name: "AI proposal" });
    await expect(cards).toHaveCount(2, { timeout: 60_000 });
    await expect(cards.first()).toContainText("Replaced by a newer proposal");
  });

  test("a diagram is proposed, rendered in the preview, applied, and shown as a picture to a viewer", async ({ browser }) => {
    test.setTimeout(150_000);
    const { a, b, roomId } = await setup(browser);
    const viewer = await signedIn(browser, seed("Vera Viewer", { roomId, role: "viewer" }));
    await viewer.page.goto(roomUrl(roomId));
    await surface(a.page).click();
    await a.page.keyboard.type("Flow of the release.");
    await expect(status(a.page)).toHaveText("Synced");
    await ask(a.page, "Propose document edit", "End of document", "Draw a diagram of the release flow.");
    await a.page.getByRole("button", { name: "Ask for an edit" }).click();
    const card = a.page.getByRole("region", { name: "AI proposal" });
    await expect(card).toContainText("Adds a diagram", { timeout: 60_000 });
    await card.getByTestId("preview-toggle").click();
    await expect(card.getByText("Diagram source")).toBeVisible();
    await card.getByRole("button", { name: "Apply" }).click();
    await a.page.getByRole("button", { name: "Apply now" }).click();
    await expect(card).toContainText("Applied", { timeout: 30_000 });
    for (const page of [a.page, b.page, viewer.page]) {
      await expect(page.getByTestId("diagram-block")).toBeVisible({ timeout: 20_000 });
    }
    // an editor can open the source; a viewer cannot
    await expect(a.page.getByTestId("diagram-edit-toggle")).toBeVisible();
    await expect(viewer.page.getByTestId("diagram-edit-toggle")).toHaveCount(0);
    await a.page.getByTestId("diagram-edit-toggle").click();
    await expect(a.page.getByTestId("diagram-source")).toContainText("flowchart TD");
    await expect(viewer.page.getByTestId("diagram-source")).toBeHidden();
  });

  test("a draft waits for the owner; an editor cannot publish; the owner publishes once and it becomes the active document", async ({ browser }) => {
    test.setTimeout(150_000);
    const { a, b, asOwner, roomId } = await setup(browser);
    await asOwner.send("POST", "/ai/consent", { notice_version: 1 });
    // the editor uploads a file and asks for a draft
    await b.page.getByLabel("Upload a file").setInputFiles({ name: "ops.md", mimeType: "text/markdown", buffer: Buffer.from(NOTES) });
    await expect(b.page.getByRole("region", { name: "Files" })).toContainText("ops.md");
    await b.page.getByRole("button", { name: "Ask AI" }).click();
    await b.page.getByRole("button", { name: "Draft new document", exact: true }).click();
    await b.page.getByText(/Files \(0 chosen\)/).click();
    await b.page.getByRole("checkbox", { name: /ops\.md/ }).check();
    await b.page.getByRole("textbox", { name: "Message" }).fill("Draft a report on the warehouse orders.");
    await b.page.getByRole("button", { name: "Ask for a draft" }).click();
    const editorCard = b.page.getByRole("region", { name: "AI proposal" });
    await expect(editorCard).toContainText("Draft of a new document", { timeout: 90_000 });
    await expect(editorCard.getByRole("button", { name: "Publish as a new document" })).toHaveCount(0);
    await expect(editorCard).toContainText("Only the room owner can publish this draft");

    // the owner (who did not ask) sees the card too, and publishes it
    const card = a.page.getByRole("region", { name: "AI proposal" });
    await expect(card).toContainText("Draft of a new document", { timeout: 30_000 });
    await card.getByRole("button", { name: "Publish as a new document" }).click();
    await expect(a.page.getByRole("alertdialog", { name: "Confirm publish" })).toContainText("makes it the room's active document");
    await a.page.getByRole("button", { name: "Create the document" }).click();
    await expect(card).toContainText("Published as a document", { timeout: 30_000 });
    const docs = await asOwner.get<{ items: { id: string; title: string; template_key: string; is_active: boolean }[] }>(`/rooms/${roomId}/documents`);
    expect(docs.items.map((d) => d.template_key).sort()).toEqual(["ai_draft", "blank"]);
    expect(docs.items.find((d) => d.template_key === "ai_draft")!.is_active).toBe(true);
    // the new document opens with the draft's content in both browsers
    await expect(surface(a.page)).toContainText("The sources describe the work.", { timeout: 30_000 });
    await expect(surface(b.page)).toContainText("The sources describe the work.", { timeout: 30_000 });
  });
});
