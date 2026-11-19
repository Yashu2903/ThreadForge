import { writeFileSync } from "node:fs";
import { type Page, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The AI workflow through the normal interface against the REAL providers (OpenAI generation, Google embeddings).
 * Run with E2E_AI=real (frontend/e2e/env.ts loads the keys from the gitignored .env, starts no stand-in and overrides
 * no base URL). Skipped otherwise: the other specs assert the stand-in's fixed text.
 *
 * Assertions are about content a real model produces (the fact, the cited source, the changed document), not wording.
 * Identifiers of every request and proposal are written to test-results/real-browser-run.json so the run can be traced
 * to the usage ledger; no text of the answers is stored there.
 */
test.skip(process.env.E2E_AI !== "real", "real-provider spec: run with E2E_AI=real");

const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");
const FACT = /twelve thousand|12,000|12000|12\s?000/i;
const FLEET = /forty|40/i;

const OPS = "# Operations notes\n\nThe warehouse processed twelve thousand orders in March. Delivery drivers were short that month.\n";
const FLEET_NOTES = "# Fleet notes\n\nThe Lisbon depot operates forty vans. The Porto depot operates nine vans.\n";

test("real inference: chosen sources, @AI cited answer, edit proposal applied, second browser, viewer denied, conflict", async ({ browser }) => {
  test.setTimeout(540_000);
  const evidence: Record<string, unknown> = { started: new Date().toISOString(), requests: [] as unknown[], proposals: [] as unknown[] };

  const owner = seed("Olivia Owner");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Real AI room");
  await asOwner.createDocument(roomId, "Launch plan");
  await asOwner.send("POST", "/ai/consent", { notice_version: 1 });
  const editor = seed("Eddie Editor", { roomId, role: "editor" });
  const asEditor = await AsUser.create(editor);
  await asEditor.send("POST", "/ai/consent", { notice_version: 1 });
  const viewer = seed("Vera Viewer", { roomId, role: "viewer" });
  const asViewer = await AsUser.create(viewer);

  const a = await signedIn(browser, owner);
  const b = await signedIn(browser, editor);
  const c = await signedIn(browser, viewer);
  for (const [p, expected] of [[a.page, "Synced"], [b.page, "Synced"], [c.page, "Read-only"]] as const) {
    await p.goto(roomUrl(roomId));
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText(expected);
  }

  // identifiers, taken from what the browser actually sent and received
  let lastRequestBody: unknown = null;
  a.page.on("request", (req) => {
    if (req.method() === "POST" && /\/rooms\/[^/]+\/ai-requests$/.test(req.url())) lastRequestBody = req.postDataJSON();
  });
  a.page.on("response", async (res) => {
    try {
      if (res.request().method() === "POST" && /\/rooms\/[^/]+\/ai-requests$/.test(res.url()) && res.status() === 201) {
        const body = (await res.json()) as { id: string; task_type?: string };
        (evidence.requests as unknown[]).push({ request_id: body.id, task_hint: (lastRequestBody as { task_hint?: string } | null)?.task_hint ?? null });
      }
      const m = /\/ai-proposals\/([0-9a-f-]{36})$/.exec(res.url());
      if (m && res.request().method() === "GET") {
        const known = (evidence.proposals as { proposal_id: string }[]).some((p) => p.proposal_id === m[1]);
        if (!known) (evidence.proposals as unknown[]).push({ proposal_id: m[1] });
      }
    } catch {
      /* a response without a body */
    }
  });

  // 1. upload two sources through the interface; choose only ONE of them for the question
  // the file input takes one file at a time, like a person choosing them
  await a.page.getByLabel("Upload a file").setInputFiles({ name: "ops.md", mimeType: "text/markdown", buffer: Buffer.from(OPS) });
  await expect(a.page.getByRole("region", { name: "Files" })).toContainText("ops.md");
  await a.page.getByLabel("Upload a file").setInputFiles({ name: "fleet.md", mimeType: "text/markdown", buffer: Buffer.from(FLEET_NOTES) });
  await expect(a.page.getByRole("region", { name: "Files" })).toContainText("fleet.md");

  await a.page.getByRole("textbox", { name: "Message" }).fill("@");
  await a.page.getByRole("option", { name: /@AI/ }).click();
  await a.page.getByText(/Files \(0 chosen\)/).click();
  await a.page.getByRole("checkbox", { name: /ops\.md/ }).check();
  await expect(a.page.getByRole("checkbox", { name: /fleet\.md/ })).not.toBeChecked();
  await a.page.getByRole("textbox", { name: "Message" }).fill("How many orders did the warehouse process in March?");
  await a.page.getByRole("button", { name: "Send to AI" }).click();

  // 2. the request is processed by the real worker and providers; a cited answer comes back
  await expect(a.page.getByRole("region", { name: "AI request" })).toBeVisible();
  const answer = a.page.getByRole("region", { name: "AI answer" });
  await expect(answer).toContainText(FACT, { timeout: 240_000 });
  await expect(a.page.getByRole("region", { name: "Files" })).toContainText("Ready");
  await answer.getByRole("button", { name: /Citation 1/ }).click();
  const cite = answer.getByRole("complementary", { name: /Citation 1/ });
  await expect(cite).toContainText("ops.md");
  await expect(cite).toContainText(FACT);
  await expect(answer).not.toContainText("fleet.md"); // the source that was not chosen is not cited
  // the second browser and the viewer see the same published answer; the viewer has no AI controls
  await expect(b.page.getByRole("region", { name: "AI answer" })).toContainText(FACT, { timeout: 30_000 });
  await expect(c.page.getByRole("region", { name: "AI answer" })).toContainText(FACT, { timeout: 30_000 });
  await expect(c.page.getByRole("button", { name: "Ask AI" })).toBeDisabled();
  evidence.answer = { cited_expected_source: true, fact_in_answer: true, unchosen_source_cited: false };

  // 3. an edit proposal: write a placeholder, select it, ask for it to be filled in from the chosen source
  await surface(a.page).click();
  await a.page.keyboard.type("March orders: TBD.");
  await expect(surface(b.page)).toContainText("March orders: TBD.");
  await expect(status(a.page)).toHaveText("Synced");
  await surface(a.page).click({ clickCount: 3 });
  await a.page.getByRole("button", { name: "Ask AI" }).click();
  await a.page.getByRole("button", { name: "Propose document edit", exact: true }).click();
  await a.page.getByRole("button", { name: "Selected blocks", exact: true }).click();
  await a.page.getByText(/Files \((\d+) chosen\)/).click();
  const ops = a.page.getByRole("checkbox", { name: /ops\.md/ });
  if (!(await ops.isChecked())) await ops.check();
  await a.page.getByRole("textbox", { name: "Message" }).fill("Replace TBD with the number of orders processed in March, taken from the operations notes. Keep the sentence short.");
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  const card = a.page.getByRole("region", { name: "AI proposal" }).first();
  await expect(card).toContainText("Proposed edit", { timeout: 240_000 });
  await expect(card).toContainText("Nothing has changed in the document");
  await expect(surface(b.page)).not.toContainText(FACT); // proposing changes nothing
  await card.getByTestId("preview-toggle").click();
  await expect(card).toContainText(FACT); // the preview shows the real model's edit
  await card.getByRole("button", { name: "Apply" }).click();
  await a.page.getByRole("button", { name: "Apply now" }).click();
  await expect(card).toContainText("Applied", { timeout: 60_000 });

  // 4. the other authenticated browser sees the applied change
  await expect(surface(a.page)).toContainText(FACT);
  await expect(surface(b.page)).toContainText(FACT, { timeout: 30_000 });
  await expect(surface(c.page)).toContainText(FACT, { timeout: 30_000 });
  evidence.applied = { first_proposal_applied: true, visible_in_second_browser: true, visible_to_viewer: true };

  // 5. the viewer cannot invoke the AI or apply a proposal (the same payload the owner's browser sent, replayed)
  const firstProposal = (evidence.proposals as { proposal_id: string }[])[0]!.proposal_id;
  const viewerApply = await asViewer.send("POST", `/rooms/${roomId}/ai-proposals/${firstProposal}/apply`, {});
  expect([403, 404]).toContain(viewerApply.status);
  const replay = { ...(lastRequestBody as Record<string, unknown>), client_request_id: crypto.randomUUID() };
  const viewerAsk = await asViewer.send("POST", `/rooms/${roomId}/ai-requests`, replay);
  expect(viewerAsk.status).toBe(403);
  evidence.viewer = { apply_status: viewerApply.status, ai_request_status: viewerAsk.status };

  // 6. a second proposal whose target is changed by another person after generation: conflict, nothing applied
  await surface(a.page).click();
  await a.page.keyboard.press("Control+End");
  await a.page.keyboard.press("Enter");
  await a.page.keyboard.type("Lisbon vans: TBD.");
  await expect(surface(b.page)).toContainText("Lisbon vans: TBD.");
  await expect(status(a.page)).toHaveText("Synced");
  await a.page.getByText("Lisbon vans: TBD.").click({ clickCount: 3 });
  await a.page.getByRole("button", { name: "Ask AI" }).click();
  await a.page.getByRole("button", { name: "Propose document edit", exact: true }).click();
  await a.page.getByRole("button", { name: "Selected blocks", exact: true }).click();
  await a.page.getByText(/Files \((\d+) chosen\)/).click();
  const fleet = a.page.getByRole("checkbox", { name: /fleet\.md/ });
  if (!(await fleet.isChecked())) await fleet.check();
  await a.page.getByRole("textbox", { name: "Message" }).fill("Replace TBD with the number of vans at the Lisbon depot, from the fleet notes.");
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  const cards = a.page.getByRole("region", { name: "AI proposal" });
  await expect(cards).toHaveCount(2, { timeout: 240_000 });
  const second = cards.nth(1);
  await expect(second).toContainText("Proposed edit", { timeout: 240_000 });
  // the other browser changes the targeted block before Apply
  await b.page.getByText("Lisbon vans: TBD.").click();
  await b.page.keyboard.press("End");
  await b.page.keyboard.type(" (Eddie edited this)");
  await expect(surface(a.page)).toContainText("Eddie edited this");
  await second.getByRole("button", { name: "Apply" }).click();
  await a.page.getByRole("button", { name: "Apply now" }).click();
  await expect(second).toContainText("The document changed after this was proposed", { timeout: 60_000 });
  await expect(surface(a.page)).toContainText("Lisbon vans: TBD. (Eddie edited this)");
  await expect(surface(b.page)).toContainText("Lisbon vans: TBD. (Eddie edited this)");
  evidence.conflict = { detected: true, document_unchanged_by_proposal: true };

  evidence.finished = new Date().toISOString();
  evidence.room_id = roomId;
  writeFileSync("test-results/real-browser-run.json", JSON.stringify(evidence, null, 2));
  await Promise.all([a.context.close(), b.context.close(), c.context.close()]);
});
