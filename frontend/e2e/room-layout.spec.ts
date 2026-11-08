import { expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

test.describe("room layout, document list and dialogs", () => {
  test("owner starts a template document, members see it, chat works beside it", async ({ browser }) => {
    const alice = seed("Alice Owner");
    const asAlice = await AsUser.create(alice);
    const roomId = await asAlice.createRoom("E2E room");
    const bob = seed("Bob Viewer", { roomId, role: "viewer" });

    const owner = await signedIn(browser, alice);
    await owner.page.goto(roomUrl(roomId));

    // empty room: the owner sees both actions
    await expect(owner.page.getByRole("heading", { name: "No document yet" })).toBeVisible();
    await owner.page.getByRole("main", { name: "Document" }).getByRole("button", { name: "Open document" }).click();
    await expect(owner.page.getByText("This room has no documents yet.")).toBeVisible();
    await expect(owner.page.getByRole("heading", { name: "Google Drive" })).toBeVisible(); // not configured here
    await owner.page.keyboard.press("Escape");

    await owner.page.getByRole("main", { name: "Document" }).getByRole("button", { name: "Start document" }).click();
    await owner.page.getByLabel("Title").fill("Launch brief");
    await owner.page.getByRole("radio", { name: /Project brief/ }).check();
    await owner.page.getByRole("button", { name: "Create document" }).click();

    await expect(owner.page.getByRole("heading", { name: "Launch brief" })).toBeVisible();
    await expect(owner.page.getByTestId("document-read").getByRole("heading", { name: "Objective" })).toBeVisible();
    await expect(owner.page.getByTestId("access-mode")).toHaveText("Access: can edit");
    await expect(owner.page.getByText("Active document")).toBeVisible();

    // a viewer opens the same room and sees the same active document, read-only
    const viewer = await signedIn(browser, bob);
    await viewer.page.goto(roomUrl(roomId));
    await expect(viewer.page.getByRole("heading", { name: "Launch brief" })).toBeVisible();
    await expect(viewer.page.getByTestId("access-mode")).toHaveText("Access: read-only");
    await expect(viewer.page.getByRole("button", { name: "Start document" })).toHaveCount(0);
    await expect(viewer.page.getByRole("button", { name: "Rename" })).toHaveCount(0);

    // chat works beside the document, in both directions, live
    await viewer.page.getByRole("textbox", { name: "Message" }).fill("hello from bob");
    await viewer.page.getByRole("button", { name: "Send" }).click();
    await expect(owner.page.getByRole("log", { name: "Messages" })).toContainText("hello from bob");

    // the owner creating a second document moves everyone's active document, live
    await asAlice.createDocument(roomId, "Second doc", "meeting_notes");
    await expect(viewer.page.getByRole("heading", { name: "Second doc" })).toBeVisible();
    await expect(viewer.page.getByTestId("document-read").getByRole("heading", { name: "Agenda" })).toBeVisible();

    await owner.context.close();
    await viewer.context.close();
    await asAlice.dispose();
  });

  test("a removed member loses the room without a reload", async ({ browser }) => {
    const alice = seed("Alice Two");
    const asAlice = await AsUser.create(alice);
    const roomId = await asAlice.createRoom("Removal room");
    const carol = seed("Carol", { roomId, role: "editor" });
    await asAlice.createDocument(roomId, "Doc");

    const member = await signedIn(browser, carol);
    await member.page.goto(roomUrl(roomId));
    await expect(member.page.getByRole("heading", { name: "Doc", exact: true })).toBeVisible();
    const removed = await asAlice.send("DELETE", `/rooms/${roomId}/members/${carol.user_id}`, undefined);
    expect(removed.status).toBe(204);
    await expect(member.page.getByRole("heading", { name: "You no longer have access to this room" })).toBeVisible();
    await member.context.close();
    await asAlice.dispose();
  });
});
