import { writeFileSync } from "node:fs";
import { type Page, type WebSocketRoute, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The unsent-text recovery (docs/ai.md 19.6) when the replacement comes from a REAL model (E2E_AI=real; see ai-real.spec.ts).
 * A second browser is "offline" (a barrier on its /collab socket) and types inside a paragraph; the owner has the real model
 * rewrite that paragraph and applies it; the second browser reconnects. Its words must be kept for it, not merged, and the
 * model's text must be intact everywhere. The recovered text is put back on request.
 */
test.skip(process.env.E2E_AI !== "real", "real-provider spec: run with E2E_AI=real");

const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");
const panel = (page: Page) => page.getByRole("region", { name: "Recovered text" });
const ORIGINAL = "Budget is forty thousand dollars.";

test("a real model's replacement of a block does not swallow another browser's unsent text", async ({ browser }) => {
  test.setTimeout(300_000);
  const evidence: Record<string, unknown> = { started: new Date().toISOString() };
  const owner = seed("Olivia Owner");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Real recovery room");
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
  for (const p of [a.page, b.page]) {
    await p.goto(roomUrl(roomId));
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText("Synced");
  }
  await surface(a.page).click();
  await a.page.keyboard.type(ORIGINAL);
  await expect(surface(b.page)).toContainText(ORIGINAL);
  await expect(status(a.page)).toHaveText("Synced");
  await expect(status(b.page)).toHaveText("Synced");

  // the second browser goes offline and types inside the paragraph
  blocked = true;
  for (const ws of open.splice(0)) void ws.close();
  await expect(status(b.page)).not.toHaveText("Synced");
  await surface(b.page).click();
  await b.page.keyboard.press("Control+End");
  await b.page.keyboard.type(" [offline note]");
  await expect(surface(a.page)).not.toContainText("[offline note]");

  // the owner has the real model rewrite that paragraph, and applies it
  await surface(a.page).click({ clickCount: 3 });
  await a.page.getByRole("button", { name: "Ask AI" }).click();
  await a.page.getByRole("button", { name: "Propose document edit", exact: true }).click();
  await a.page.getByRole("button", { name: "Selected blocks", exact: true }).click();
  await a.page.getByRole("textbox", { name: "Message" }).fill("Rewrite this sentence in a formal tone, using different words and writing out the amount in figures with a currency sign.");
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  const card = a.page.getByRole("region", { name: "AI proposal" });
  await expect(card).toContainText("Proposed edit", { timeout: 240_000 });
  await card.getByRole("button", { name: "Apply" }).click();
  await a.page.getByRole("button", { name: "Apply now" }).click();
  await expect(card).toContainText("Applied", { timeout: 60_000 });
  await expect(surface(a.page)).not.toContainText(ORIGINAL); // the real model's text replaced it
  const replaced = (await surface(a.page).innerText()).trim();
  evidence.model_text_differs_from_original = replaced !== ORIGINAL;

  // the second browser comes back
  blocked = false;
  await expect(surface(b.page)).not.toContainText(ORIGINAL, { timeout: 60_000 });
  await expect(status(b.page)).toHaveText("Synced", { timeout: 60_000 });

  // the model's text is intact in both browsers, the offline words are in neither document, and only the typist is told
  expect((await surface(b.page).innerText()).trim()).toBe(replaced);
  await expect(surface(a.page)).not.toContainText("[offline note]");
  await expect(surface(b.page)).not.toContainText("[offline note]");
  await expect(panel(a.page)).toHaveCount(0);
  await expect(panel(b.page)).toBeVisible();
  await expect(panel(b.page).getByLabel("Your unsent text")).toHaveValue(" [offline note]");

  // explicit recovery: the person puts it back; both browsers converge; once only
  await panel(b.page).getByRole("button", { name: "Insert into the document" }).click();
  await expect(surface(b.page)).toContainText("[offline note]");
  await expect(surface(a.page)).toContainText("[offline note]");
  await expect(surface(a.page).getByText("[offline note]")).toHaveCount(1);
  await expect(panel(b.page)).toHaveCount(0);
  evidence.recovered_once = true;
  evidence.finished = new Date().toISOString();
  writeFileSync("test-results/real-recovery-run.json", JSON.stringify(evidence, null, 2));
  await Promise.all([a.context.close(), b.context.close()]);
});
