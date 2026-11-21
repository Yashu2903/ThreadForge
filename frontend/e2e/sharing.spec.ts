import { type Page, expect, test } from "@playwright/test";
import { WEB_ORIGIN } from "./env";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The room-sharing workflow as two separate people in two separate browsers: the owner creates a room, invites with a link,
 * the other account joins from that link, both work in the same document and chat, then permissions change under them.
 * Screenshots of the real application are written to test-results/screens (not committed).
 */
const SHOTS = "test-results/screens";
const shot = (page: Page, name: string) => page.screenshot({ path: `${SHOTS}/${name}.png` });

async function noPageOverflow(page: Page, where: string) {
  const m = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    iw: window.innerWidth,
    sh: document.documentElement.scrollHeight,
    ih: window.innerHeight,
    bodySw: document.body.scrollWidth,
  }));
  expect(m.sw, `${where}: horizontal page overflow`).toBeLessThanOrEqual(m.iw);
  expect(m.bodySw, `${where}: body overflow`).toBeLessThanOrEqual(m.iw);
  expect(m.sh, `${where}: the page itself scrolls`).toBeLessThanOrEqual(m.ih);
}

test.describe("invite, join and permissions: two people", () => {
  test("owner invites, a second account joins, roles and removal take effect live", async ({ browser }) => {
    const alice = seed("Alice Owner");
    const bob = seed("Bob Guest");
    const asAlice = await AsUser.create(alice);
    const asBob = await AsUser.create(bob);

    const owner = await signedIn(browser, alice);
    await owner.page.setViewportSize({ width: 1440, height: 900 });
    await owner.page.goto("/#/");

    // 1. the owner creates a room from the rail and sees it in the room list
    await expect(owner.page.getByRole("heading", { name: "Your rooms" })).toBeVisible();
    await expect(owner.page.getByRole("heading", { name: "You are not in any room yet" })).toBeVisible();
    await shot(owner.page, "01-room-home-empty");
    await owner.page.getByRole("button", { name: "Create a room" }).first().click();
    await owner.page.getByLabel("Room name").fill("Launch team");
    await owner.page.getByRole("button", { name: "Create room" }).click();
    await expect(owner.page).toHaveURL(/#\/rooms\/[0-9a-f-]{36}$/);
    const roomId = owner.page.url().split("/rooms/")[1]!;
    await expect(owner.page.getByRole("button", { name: "Launch team" }).first()).toBeVisible();

    // a document both people will edit
    const doc = await asAlice.createDocument(roomId, "Shared plan");
    await expect(owner.page.getByRole("heading", { name: "Shared plan" })).toBeVisible();

    // 2. Invite people is visible; generate an Editor invitation; the link is the one the server built
    await owner.page.getByTestId("invite-people").click();
    await expect(owner.page.getByRole("dialog", { name: "Invite people" })).toBeVisible();
    await shot(owner.page, "02-invite-dialog");
    await owner.page.getByRole("radio", { name: /Editor/ }).check();
    await owner.page.getByRole("button", { name: "Generate invitation" }).click();
    const link = await owner.page.getByTestId("invite-link").inputValue();
    const key = await owner.page.getByTestId("invite-key").inputValue();
    expect(link).toMatch(/\/#\/join\/[A-Za-z0-9_-]{43}$/);
    expect(link.endsWith(key)).toBe(true);
    await shot(owner.page, "03-invite-created");
    await owner.page.getByRole("button", { name: "Done" }).click();

    // 3. the second person opens the link while signed out: the invitation survives the sign-in and the address bar is cleaned
    const guestContext = await browser.newContext({ baseURL: WEB_ORIGIN, viewport: { width: 1440, height: 900 } });
    const guest = await guestContext.newPage();
    await guest.goto(link.replace(/^https?:\/\/[^/]+/, WEB_ORIGIN));
    await expect(guest.getByTestId("signin")).toContainText("You have been invited to a room");
    await expect(guest).toHaveURL(`${WEB_ORIGIN}/#/join`); // the key is no longer in the address bar
    await shot(guest, "04-join-signed-out");
    await guestContext.addCookies([{ name: bob.cookie_name, value: bob.secret, url: WEB_ORIGIN }]); // "signing in"
    await guest.goto("/"); // where the real sign-in lands: the invitation, kept in this tab, takes over
    await expect(guest.getByTestId("join-page")).toBeVisible();
    await expect(guest.getByRole("heading", { name: "Join Launch team?" })).toBeVisible();
    await expect(guest.getByText(/an editor/)).toBeVisible();
    await shot(guest, "05-join-preview");
    // nothing has happened yet: Bob is not a member
    expect((await asBob.getStatus(`/rooms/${roomId}`)).status).toBeGreaterThanOrEqual(403);
    await guest.getByTestId("join-room").click();
    await expect(guest).toHaveURL(new RegExp(`#/rooms/${roomId}$`));
    await expect(guest.getByRole("heading", { name: "Shared plan" })).toBeVisible();
    await expect(guest.getByTestId("invite-people")).toHaveCount(0); // only the owner invites

    // 4. both see the right members (the owner's drawer updates live)
    await owner.page.getByTestId("toggle-members").click();
    const drawer = owner.page.getByTestId("members-drawer");
    await expect(drawer.getByTestId(`member-${bob.user_id}`)).toContainText("Bob Guest");
    await expect(drawer.getByTestId(`member-${alice.user_id}`)).toContainText("Alice Owner");
    await expect(drawer.getByTestId(`member-${bob.user_id}`)).toContainText("Online");
    await shot(owner.page, "06-members-drawer");
    await guest.getByTestId("toggle-members").click();
    await expect(guest.getByTestId("members-drawer").getByTestId(`member-${alice.user_id}`)).toContainText("Owner");
    await guest.getByRole("button", { name: "Close members" }).click();

    // 5. chat both ways (the owner's members drawer sits on top of the chat, so it is closed while chatting)
    await owner.page.getByRole("button", { name: "Close members" }).click();
    await guest.getByRole("textbox", { name: "Message" }).fill("hi from bob");
    await guest.getByRole("button", { name: "Send", exact: true }).click();
    await expect(owner.page.getByRole("log", { name: "Messages" })).toContainText("hi from bob");
    await owner.page.getByRole("textbox", { name: "Message" }).fill("welcome bob");
    await owner.page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(guest.getByRole("log", { name: "Messages" })).toContainText("welcome bob");

    // 6. both edit the same document and see each other's text
    await owner.page.getByTestId("editor").locator(".tf-editor").click();
    await owner.page.keyboard.type("Owner line. ");
    await guest.getByTestId("editor").locator(".tf-editor").click();
    await guest.keyboard.type("Guest line.");
    await expect(owner.page.getByTestId("editor")).toContainText("Guest line.");
    await expect(guest.getByTestId("editor")).toContainText("Owner line.");
    await expect(guest.getByTestId("sync-status")).toHaveText(/Saved|Synced|saved/i);
    await shot(guest, "07-guest-editing");

    await owner.page.getByTestId("toggle-members").click();
    // 7. the owner changes Bob to Viewer: controls become read-only live, and the AI is not available to them
    await drawer.getByLabel("Role of Bob Guest").selectOption("viewer");
    await expect(guest.getByTestId("access-mode")).toHaveText("Access: read-only");
    await expect(guest.getByRole("toolbar", { name: "Formatting" })).toHaveCount(0);
    await expect(guest.getByTestId("editor").locator(".tf-editor")).toHaveAttribute("aria-readonly", "true");
    await expect(guest.getByRole("button", { name: "Ask AI" })).toBeDisabled();
    await expect(guest.getByText(/AI requests are for editors and owners/)).toBeVisible();
    const denied = await asBob.send("PATCH", `/rooms/${roomId}/documents/${doc.id}`, { title: "Bob renamed it" });
    expect(denied.status).toBeGreaterThanOrEqual(403);
    await shot(guest, "08-guest-viewer");

    // 8. the owner removes Bob: Bob loses the room without reloading; the API says no too
    await drawer.getByRole("button", { name: "Remove Bob Guest" }).click();
    await owner.page.getByRole("dialog").getByRole("button", { name: /Remove/ }).click();
    await expect(guest.getByRole("heading", { name: "You no longer have access to this room" })).toBeVisible();
    expect((await asBob.getStatus(`/rooms/${roomId}`)).status).toBeGreaterThanOrEqual(403);
    await expect(drawer.getByTestId(`member-${bob.user_id}`)).toHaveCount(0);

    // 9. the used invitation cannot bring Bob back; a revoked one fails the same way
    await guest.goto(`/#/join/${key}`);
    await expect(guest.getByTestId("invitation-invalid")).toBeVisible();
    await shot(guest, "09-join-invalid");
    await drawer.getByRole("tab", { name: "Invitations" }).click();
    await expect(drawer.getByText("Used")).toBeVisible();
    await owner.page.getByRole("button", { name: "Invite people" }).first().click();
    await owner.page.getByRole("radio", { name: /Viewer/ }).check();
    await owner.page.getByRole("button", { name: "Generate invitation" }).click();
    const key2 = await owner.page.getByTestId("invite-key").inputValue();
    await owner.page.getByRole("button", { name: "Done" }).click();
    await drawer.getByRole("button", { name: "Revoke" }).first().click();
    await guest.goto(`/#/join/${key2}`);
    await expect(guest.getByTestId("invitation-invalid")).toBeVisible();

    await owner.context.close();
    await guestContext.close();
    await asAlice.dispose();
    await asBob.dispose();
  });

  test("the layout holds at five screen sizes with long names and many files", async ({ browser }) => {
    const alice = seed("Alexandria Konstantinopoulos-Featherstonehaugh");
    const asAlice = await AsUser.create(alice);
    const roomId = await asAlice.createRoom("A very long room name that keeps going and going to test truncation");
    const doc = await asAlice.createDocument(roomId, "An equally long document title that should be cut with an ellipsis not wrapped");
    await asAlice.createDocument(roomId, "Second");
    await asAlice.createDocument(roomId, "Third");
    void doc;
    for (let i = 0; i < 6; i++) {
      await asAlice.uploadSource(roomId, `quarterly-operations-review-with-a-really-long-file-name-${i}.md`, `# Review ${i}
Notes.`);
    }
    const owner = await signedIn(browser, alice);
    const sizes = [
      [1366, 768],
      [1440, 900],
      [1920, 1080],
      [768, 1024],
      [390, 844],
    ] as const;
    await owner.page.setViewportSize({ width: 1440, height: 900 });
    await owner.page.goto("/#/");
    for (const [w, h] of sizes) {
      await owner.page.setViewportSize({ width: w, height: h });
      await owner.page.goto("/#/");
      await owner.page.reload();
      await expect(owner.page.getByRole("heading", { name: "Your rooms" })).toBeVisible();
      await noPageOverflow(owner.page, `home ${w}x${h}`);
      await shot(owner.page, `home-${w}x${h}`);

      await owner.page.goto(roomUrl(roomId));
      await owner.page.reload();
      await expect(owner.page.getByRole("heading", { name: "Third", exact: true })).toBeVisible();
      await expect(owner.page.getByTestId("editor")).toBeVisible();
      await noPageOverflow(owner.page, `room ${w}x${h}`);
      await shot(owner.page, `room-${w}x${h}`);
      if (w <= 900) {
        await owner.page.getByRole("tab", { name: "Chat" }).click();
        await expect(owner.page.getByRole("textbox", { name: "Message" })).toBeVisible();
        await noPageOverflow(owner.page, `chat tab ${w}x${h}`);
        await shot(owner.page, `chat-${w}x${h}`);
        await owner.page.getByRole("tab", { name: "Document" }).click();
        await owner.page.getByRole("button", { name: "Open navigation" }).click();
        await noPageOverflow(owner.page, `drawer ${w}x${h}`);
        await shot(owner.page, `drawer-${w}x${h}`);
        await owner.page.keyboard.press("Escape");
      }
      // the composer is visible without scrolling anything
      if (w > 900) await expect(owner.page.getByRole("textbox", { name: "Message" })).toBeInViewport();
    }
    await owner.context.close();
    await asAlice.dispose();
  });
});
