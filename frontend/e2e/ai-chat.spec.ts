import { expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * AI phase 1 in a real browser, against the real API, the real AI worker and the real collaboration
 * service; only the two providers are a local stand-in (backend/scripts/e2e_ai_stub.py).
 *
 * The flow: an editor accepts the data notice, uploads a file (nothing is analyzed yet), chooses
 * `@AI`, ticks the file and asks. The worker analyzes the file and answers; the editor sees a cited
 * answer card, a viewer sees the same card but none of the AI controls, and text that merely contains
 * "@AI" is an ordinary message that triggers nothing.
 */
const NOTES = "# Operations\n\nThe warehouse processed twelve thousand orders in March. Delivery drivers were short.\n";

test.describe("AI in the browser", () => {
  test("upload, ask with @AI, get a cited answer; viewers see it read-only", async ({ browser }) => {
    test.setTimeout(120_000);
    const alice = seed("Alice AI");
    const asAlice = await AsUser.create(alice);
    const roomId = await asAlice.createRoom("AI room");
    await asAlice.createDocument(roomId, "Plan");
    const bob = seed("Bob Viewer", { roomId, role: "viewer" });

    const owner = await signedIn(browser, alice);
    const page = owner.page;
    await page.goto(roomUrl(roomId));
    await expect(page.getByRole("heading", { name: "Plan" })).toBeVisible();

    // the data notice first
    await page.getByRole("button", { name: "AI settings" }).click();
    await expect(page.getByText(/sent to the embedding provider/)).toBeVisible();
    await page.getByRole("button", { name: /I have read it and agree/ }).click();
    await expect(page.getByText("You accepted this notice.")).toBeVisible();
    await page.getByRole("dialog", { name: "AI settings" }).getByRole("button", { name: "Close" }).click();

    // upload: stored and inspected, NOT analyzed
    await page.getByLabel("Upload a file").setInputFiles({ name: "ops.md", mimeType: "text/markdown", buffer: Buffer.from(NOTES) });
    await expect(page.getByRole("region", { name: "Files" })).toContainText("Not analyzed yet");

    // typed "@AI" in an ordinary message triggers nothing
    await page.getByRole("textbox", { name: "Message" }).fill("note to self about @AI later");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByRole("log", { name: "Messages" })).toContainText("note to self about @AI later");
    const plain = await asAlice.get<{ items: { kind: string }[] }>(`/rooms/${roomId}/messages`);
    expect(plain.items.map((m) => m.kind)).toEqual(["user"]);

    // choose the mention from the menu, tick the file, ask
    await page.getByRole("textbox", { name: "Message" }).fill("@");
    await page.getByRole("option", { name: /@AI/ }).click();
    await expect(page.getByTestId("ai-chip")).toBeVisible();
    await page.getByText(/Files \(0 chosen\)/).click();
    await page.getByRole("checkbox", { name: /ops\.md/ }).check();
    await expect(page.getByText(/will be analyzed when you send/)).toBeVisible();
    await page.getByRole("textbox", { name: "Message" }).fill("How many orders did the warehouse process?");
    await page.getByRole("button", { name: "Send to AI" }).click();

    // the request card, then the answer card
    await expect(page.getByRole("region", { name: "AI request" })).toBeVisible();
    const answer = page.getByRole("region", { name: "AI answer" });
    await expect(answer).toContainText("The evidence covers the request.", { timeout: 60_000 });
    await expect(page.getByRole("region", { name: "Files" })).toContainText("Ready");

    // the citation opens the passage it points to
    await answer.getByRole("button", { name: "Citation 1" }).click();
    const cite = answer.getByRole("complementary", { name: "Citation 1" });
    await expect(cite).toContainText("ops.md");
    await expect(cite).toContainText("twelve thousand orders");
    await expect(cite).toContainText("does not prove");

    // a viewer sees the same published answer, but no AI controls and no request progress
    const viewer = await signedIn(browser, bob);
    await viewer.page.goto(roomUrl(roomId));
    await expect(viewer.page.getByRole("region", { name: "AI answer" })).toContainText("The evidence covers the request.");
    await expect(viewer.page.getByRole("button", { name: "Ask AI" })).toBeDisabled();
    await expect(viewer.page.getByText(/AI requests are for editors and owners/)).toBeVisible();
    await expect(viewer.page.getByRole("region", { name: "AI request" })).toHaveCount(0);

    // the chat now holds exactly: the plain note, the @AI message, the AI's reply
    const all = await asAlice.get<{ items: { kind: string; author_display_name: string }[] }>(`/rooms/${roomId}/messages`);
    expect(all.items.map((m) => m.kind)).toEqual(["user", "ai_request", "ai_answer"]);
    expect(all.items[2]!.author_display_name).toBe("ThreadForge AI");

    await owner.context.close();
    await viewer.context.close();
    await asAlice.dispose();
  });

  test("the owner can delete an AI answer and the card says it was removed", async ({ browser }) => {
    test.setTimeout(120_000);
    const alice = seed("Alice Delete");
    const asAlice = await AsUser.create(alice);
    const roomId = await asAlice.createRoom("AI delete room");
    await asAlice.createDocument(roomId, "Plan");
    await asAlice.send("POST", "/ai/consent", { notice_version: 1 });
    const owner = await signedIn(browser, alice);
    const page = owner.page;
    await page.goto(roomUrl(roomId));
    await page.getByLabel("Upload a file").setInputFiles({ name: "ops.md", mimeType: "text/markdown", buffer: Buffer.from(NOTES) });
    await expect(page.getByRole("region", { name: "Files" })).toContainText("ops.md");
    await page.getByRole("button", { name: "Ask AI" }).click();
    await page.getByText(/Files \(0 chosen\)/).click();
    await page.getByRole("checkbox", { name: /ops\.md/ }).check();
    await page.getByRole("textbox", { name: "Message" }).fill("How many orders did the warehouse process?");
    await page.getByRole("button", { name: "Send to AI" }).click();
    await expect(page.getByRole("region", { name: "AI answer" })).toContainText("The evidence covers the request.", { timeout: 60_000 });

    page.on("dialog", (d) => void d.accept());
    await page.getByRole("button", { name: "Delete this answer" }).click();
    await expect(page.getByRole("log", { name: "Messages" })).toContainText("This message was deleted.");
    await owner.context.close();
    await asAlice.dispose();
  });
});
