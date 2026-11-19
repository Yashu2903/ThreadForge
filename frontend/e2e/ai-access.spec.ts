import { type Page, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * Losing write access stops AI invocation and AI application, in two real sessions against the real API,
 * collaboration service and PostgreSQL (the AI provider is the local stand-in: nothing here is about model quality).
 * The server decides: the proposal stays pending and the document does not change.
 */
const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");

test("an editor who loses write access can neither invoke the AI nor apply a proposal they requested", async ({ browser }) => {
  test.setTimeout(180_000);
  const owner = seed("Olivia Owner");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Access room");
  await asOwner.createDocument(roomId, "Launch plan");
  await asOwner.send("POST", "/ai/consent", { notice_version: 1 });
  const editor = seed("Eddie Editor", { roomId, role: "editor" });
  const asEditor = await AsUser.create(editor);
  await asEditor.send("POST", "/ai/consent", { notice_version: 1 });
  const a = await signedIn(browser, owner);
  const b = await signedIn(browser, editor);
  for (const p of [a.page, b.page]) {
    await p.goto(roomUrl(roomId));
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText("Synced");
  }

  // the proposal id, taken from the request the card makes
  let proposalId = "";
  b.page.on("response", (res) => {
    const m = /\/ai-proposals\/([0-9a-f-]{36})$/.exec(res.url());
    if (m && res.request().method() === "GET") proposalId = m[1]!;
  });

  await surface(b.page).click();
  await b.page.keyboard.type("Budget is forty thousand dollars.");
  await expect(surface(a.page)).toContainText("Budget is forty thousand dollars.");
  await expect(status(b.page)).toHaveText("Synced");

  // 1. while they may write, the editor asks for an edit and gets a proposal card
  await surface(b.page).click({ clickCount: 3 });
  await b.page.getByRole("button", { name: "Ask AI" }).click();
  await b.page.getByRole("button", { name: "Propose document edit", exact: true }).click();
  await b.page.getByRole("button", { name: "Selected blocks", exact: true }).click();
  await b.page.getByRole("textbox", { name: "Message" }).fill("Make this clearer.");
  await b.page.getByRole("button", { name: "Ask for an edit" }).click();
  const card = b.page.getByRole("region", { name: "AI proposal" });
  await expect(card).toContainText("Proposed edit", { timeout: 60_000 });
  await expect.poll(() => proposalId, { timeout: 15_000 }).not.toBe("");

  // 2. the owner takes their write access away
  const demoted = await asOwner.send("PATCH", `/rooms/${roomId}/members/${editor.user_id}`, { role: "viewer" });
  expect(demoted.status).toBe(200);

  // 3. the server refuses to apply it for them, and refuses a new AI request
  const apply = await asEditor.send("POST", `/rooms/${roomId}/ai-proposals/${proposalId}/apply`, {});
  expect(apply.status).toBe(403);
  const regenerate = await asEditor.send("POST", `/rooms/${roomId}/ai-proposals/${proposalId}/regenerate`, { client_request_id: crypto.randomUUID() });
  expect(regenerate.status).toBe(403);

  // 4. nothing changed: the document is as it was and the proposal is still pending
  await expect(surface(a.page)).toContainText("Budget is forty thousand dollars.");
  await expect(surface(a.page)).not.toContainText("Rewritten by the assistant.");
  const still = await asOwner.get<{ state: string }>(`/rooms/${roomId}/ai-proposals/${proposalId}`);
  expect(still.state).toBe("pending");

  // 5. in their browser the AI button is disabled with an explanation, and the document is read-only
  await expect(b.page.getByRole("button", { name: "Ask AI" })).toBeDisabled({ timeout: 15_000 });
  await expect(b.page.getByText("AI requests are for editors and owners.")).toBeVisible();
  await expect(b.page.getByTestId("access-mode")).toHaveText("Access: read-only", { timeout: 15_000 });
  await b.page.reload();
  await expect(surface(b.page)).toBeVisible();
  await expect(b.page.getByRole("region", { name: "AI proposal" })).toHaveCount(0); // a viewer sees only the plain chat message
  await expect(b.page.getByRole("button", { name: "Apply" })).toHaveCount(0);

  // 6. the proposal itself is fine: the owner, who still may write, can apply it, and both sessions see the result
  const ownerApply = await asOwner.send("POST", `/rooms/${roomId}/ai-proposals/${proposalId}/apply`, {});
  expect(ownerApply.status, JSON.stringify(ownerApply.body)).toBe(200);
  await expect(surface(a.page)).toContainText("Rewritten by the assistant.", { timeout: 30_000 });
  await expect(surface(b.page)).toContainText("Rewritten by the assistant.", { timeout: 30_000 });
});
