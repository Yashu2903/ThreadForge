import { type Browser, type Page, type WebSocketRoute, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The unsent/offline edit inside a block the AI replaced (docs/ai.md 19.6), in two real browsers against the real API,
 * collaboration service and PostgreSQL. The AI provider is the local stand-in (backend/scripts/e2e_ai_stub.py): this
 * proves the collaboration and recovery path, nothing about a real model.
 *
 * "Offline" is a barrier on the second browser's /collab WebSocket (Playwright routeWebSocket): while it is closed,
 * the socket is refused again as soon as it is re-opened; opening the barrier lets the next reconnect through.
 */
const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");
const panel = (page: Page) => page.getByRole("region", { name: "Recovered text" });

async function offlineReplacement(browser: Browser) {
  const owner = seed("Olivia Owner");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Recovery room");
  await asOwner.createDocument(roomId, "Launch plan");
  await asOwner.send("POST", "/ai/consent", { notice_version: 1 });
  const editor = seed("Eddie Editor", { roomId, role: "editor" });
  const asEditor = await AsUser.create(editor);
  await asEditor.send("POST", "/ai/consent", { notice_version: 1 });
  const a = await signedIn(browser, owner);
  const b = await signedIn(browser, editor);

  let blocked = false;
  const open: WebSocketRoute[] = [];
  await b.page.routeWebSocket(/\/collab/, (ws) => {
    if (blocked) return void ws.close();
    ws.connectToServer();
    open.push(ws);
  });
  const network = {
    down: () => {
      blocked = true;
      for (const ws of open.splice(0)) void ws.close();
    },
    up: () => {
      blocked = false;
    },
  };

  for (const p of [a.page, b.page]) {
    await p.goto(roomUrl(roomId));
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText("Synced");
  }
  await surface(a.page).click();
  await a.page.keyboard.type("Budget is forty thousand dollars.");
  await expect(surface(b.page)).toContainText("Budget is forty thousand dollars.");
  await expect(status(a.page)).toHaveText("Synced");
  await expect(status(b.page)).toHaveText("Synced");

  // 1. the second browser goes offline and types INSIDE the paragraph the AI is about to replace
  network.down();
  await expect(status(b.page)).not.toHaveText("Synced");
  await surface(b.page).click();
  await b.page.keyboard.press("Control+End");
  await b.page.keyboard.type(" [offline note]");
  await expect(surface(b.page)).toContainText("[offline note]");
  await expect(surface(a.page)).not.toContainText("[offline note]"); // the server never saw it

  // 2. meanwhile the owner asks for a rewrite of that paragraph and applies it (the base hash still matches)
  await surface(a.page).click({ clickCount: 3 });
  await a.page.getByRole("button", { name: "Ask AI" }).click();
  await a.page.getByRole("button", { name: "Propose document edit", exact: true }).click();
  await a.page.getByRole("button", { name: "Selected blocks", exact: true }).click();
  await a.page.getByRole("textbox", { name: "Message" }).fill("Make this clearer.");
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  const card = a.page.getByRole("region", { name: "AI proposal" });
  await expect(card).toContainText("Proposed edit", { timeout: 60_000 });
  await card.getByRole("button", { name: "Apply" }).click();
  await a.page.getByRole("button", { name: "Apply now" }).click();
  await expect(card).toContainText("Applied", { timeout: 30_000 });
  await expect(surface(a.page)).toContainText("Rewritten by the assistant.");

  // 3. the second browser comes back
  network.up();
  await expect(surface(b.page)).toContainText("Rewritten by the assistant.", { timeout: 60_000 });
  await expect(status(b.page)).toHaveText("Synced", { timeout: 60_000 });
  return { a, b, roomId, asOwner };
}

test.describe("unsent text inside a block the AI replaced", () => {
  test("is kept for the person who typed it, never merged over the AI's text, and can be put back on request", async ({ browser }) => {
    test.setTimeout(240_000);
    const { a, b } = await offlineReplacement(browser);

    // both browsers show the accepted AI text, and neither shows the old words in the document
    for (const p of [a.page, b.page]) {
      await expect(surface(p)).toContainText("Rewritten by the assistant.");
      await expect(surface(p)).not.toContainText("[offline note]");
    }
    // only the person who typed it is told, and can read it
    await expect(panel(a.page)).toHaveCount(0);
    await expect(panel(b.page)).toBeVisible();
    await expect(panel(b.page)).toContainText("reached the server when the shared document changed");
    await expect(panel(b.page).getByLabel("Your unsent text")).toHaveValue(" [offline note]");

    // nothing was put back automatically; the person does it on purpose
    await panel(b.page).getByRole("button", { name: "Insert into the document" }).click();
    await expect(surface(b.page)).toContainText("[offline note]");
    await expect(surface(a.page)).toContainText("[offline note]"); // a new paragraph, shared like any edit
    await expect(surface(a.page)).toContainText("Rewritten by the assistant.");
    await expect(panel(b.page)).toHaveCount(0);
    // once only
    await expect(surface(b.page).getByText("[offline note]")).toHaveCount(1);
  });

  test("survives a reload, and is discarded only after a confirmation", async ({ browser }) => {
    test.setTimeout(240_000);
    const { b } = await offlineReplacement(browser);
    await expect(panel(b.page)).toBeVisible();

    await b.page.reload();
    await expect(surface(b.page)).toBeVisible();
    await expect(panel(b.page)).toBeVisible(); // kept on this device
    await expect(panel(b.page).getByLabel("Your unsent text")).toHaveValue(" [offline note]");

    await panel(b.page).getByRole("button", { name: "Discard…" }).click();
    await expect(panel(b.page)).toContainText("Discard this text for good?");
    await panel(b.page).getByRole("button", { name: "Keep it" }).click();
    await expect(panel(b.page)).toBeVisible();
    await panel(b.page).getByRole("button", { name: "Discard…" }).click();
    await panel(b.page).getByRole("button", { name: "Yes, discard it" }).click();
    await expect(panel(b.page)).toHaveCount(0);
    await b.page.reload();
    await expect(surface(b.page)).toBeVisible();
    await expect(panel(b.page)).toHaveCount(0); // and gone for good
    await expect(surface(b.page)).not.toContainText("[offline note]");
  });
});
