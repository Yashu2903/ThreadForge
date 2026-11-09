import { type Page, type WebSocketRoute, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The real stack: API + PostgreSQL + collaboration service + Vite + real browsers. Nothing in the
 * path from keyboard to database is faked.
 */

const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");
/** The document's own text: remote caret name tags are drawn inside the editor but are not text. */
const textOf = (page: Page) =>
  page.evaluate(() => {
    const clone = document.querySelector(".tf-editor")!.cloneNode(true) as HTMLElement;
    clone.querySelectorAll(".collab-caret").forEach((n) => n.remove());
    return clone.textContent ?? "";
  });

/**
 * Sit between one browser tab and the collaboration service so a test can (a) cut the connection and
 * (b) keep the tab connected while its messages stop reaching the server. Works the same in every
 * browser engine (context.setOffline does not close an open WebSocket in Firefox).
 */
async function controlCollab(page: Page) {
  const live = new Set<WebSocketRoute>();
  let offline = false;
  let blockUp = false;
  let attempts = 0;
  await page.routeWebSocket(/\/collab/, (ws) => {
    attempts += 1;
    if (offline) return void ws.close({ code: 1006 });
    const server = ws.connectToServer();
    live.add(ws);
    ws.onMessage((m) => {
      if (!blockUp) server.send(m);
    });
    server.onMessage((m) => ws.send(m));
    ws.onClose(() => live.delete(ws));
  });
  return {
    offline(on: boolean) {
      offline = on;
      if (on) for (const ws of live) ws.close({ code: 1006 });
    },
    blockUp(on: boolean) {
      blockUp = on;
    },
    /** refuse NEW connections without touching the open ones */
    refuseNew(on: boolean) {
      offline = on;
    },
    /** the server ends the connection with an application close code (e.g. 4403) */
    closeWith(code: number) {
      for (const ws of live) ws.close({ code, reason: "test" });
    },
    get attempts() {
      return attempts;
    },
  };
}

async function setup(browser: import("@playwright/test").Browser, withViewer = false) {
  const owner = seed("Olivia");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Collab room");
  const doc = await asOwner.createDocument(roomId, "Shared plan");
  const editor = seed("Eddie", { roomId, role: "editor" });
  const viewer = withViewer ? seed("Vera", { roomId, role: "viewer" }) : null;
  const a = await signedIn(browser, owner);
  const b = await signedIn(browser, editor);
  const c = viewer ? await signedIn(browser, viewer) : null;
  for (const p of [a.page, b.page, ...(c ? [c.page] : [])]) await p.goto(roomUrl(roomId));
  for (const p of [a.page, b.page]) {
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText("Synced");
  }
  return { roomId, doc, asOwner, a, b, c };
}

test.describe("live collaborative editing", () => {
  test("two people edit one document and see each other; a viewer sees it read-only", async ({ browser }) => {
    const { a, b, c } = await setup(browser, true);
    await surface(a.page).click();
    await a.page.keyboard.type("Hello from Olivia");
    await expect(surface(b.page)).toContainText("Hello from Olivia");
    await surface(b.page).click();
    await b.page.keyboard.press("End");
    await b.page.keyboard.type(" and Eddie");
    await expect(surface(a.page)).toContainText("Hello from Olivia and Eddie");

    // "Synced" means the server has it
    await expect(status(a.page)).toHaveText("Synced");
    await expect(status(b.page)).toHaveText("Synced");

    // a viewer follows live but cannot type
    const viewerPage = c!.page;
    await expect.poll(() => textOf(viewerPage)).toContain("Hello from Olivia and Eddie");
    await expect(status(viewerPage)).toHaveText("Read-only");
    await expect(surface(viewerPage)).toHaveAttribute("contenteditable", "false");
    await expect(surface(viewerPage)).toHaveAttribute("aria-readonly", "true");
    await expect(viewerPage.getByRole("toolbar")).toHaveCount(0);
    await surface(viewerPage).click();
    await viewerPage.keyboard.type("X");
    expect(await textOf(viewerPage)).not.toContain("X");
    expect(await textOf(a.page)).not.toContain("Hello from Olivia and EddieX");
  });

  test("a collaborator's caret is labelled from the member list", async ({ browser }) => {
    const { a, b } = await setup(browser);
    await surface(a.page).click();
    await a.page.keyboard.type("caret test");
    await expect(surface(b.page)).toContainText("caret test");
    await expect(b.page.locator(".collab-caret-label").first()).toHaveText("Olivia");
  });

  test("text survives a reload: it comes back from the database, not from the page", async ({ browser }) => {
    const { a, roomId } = await setup(browser);
    await surface(a.page).click();
    await a.page.keyboard.type("Stored text");
    await expect(status(a.page)).toHaveText("Synced");
    await a.page.waitForTimeout(1500); // the service writes to PostgreSQL a moment after the server has it
    const fresh = await signedIn(browser, seed("Fresh", { roomId, role: "editor" }));
    await fresh.page.goto(roomUrl(roomId));
    await expect(surface(fresh.page)).toContainText("Stored text");
  });

  test("toolbar: bold, heading, list, table, link (and a refused link), undo", async ({ browser }) => {
    const { a, b } = await setup(browser);
    const page = a.page;
    await surface(page).click();
    await page.keyboard.type("Plain words");
    await page.keyboard.press("Control+A");
    await page.getByTestId("tb-bold").click();
    await expect(page.getByTestId("tb-bold")).toHaveAttribute("aria-pressed", "true");
    await expect(surface(b.page).locator("strong")).toContainText("Plain words");

    await page.getByTestId("tb-block").selectOption("h2");
    await expect(surface(page).locator("h2")).toContainText("Plain words");
    await expect(surface(b.page).locator("h2")).toContainText("Plain words");

    await page.keyboard.press("Control+End");
    await page.keyboard.press("Enter");
    await page.getByTestId("tb-bullets").click();
    await page.keyboard.type("item one");
    await expect(surface(b.page).locator("ul li")).toContainText("item one");

    // table
    await page.getByTestId("tb-table").click();
    await page.getByTestId("table-rows").fill("2");
    await page.getByTestId("table-cols").fill("2");
    await page.getByTestId("table-insert").click();
    await expect(surface(page).locator("table tr")).toHaveCount(2);
    await expect(surface(b.page).locator("table tr")).toHaveCount(2);
    await expect(page.getByTestId("tb-row-below")).toBeVisible();

    // links: a refused scheme is explained and nothing changes, a valid one applies
    await surface(page).locator("h2").click({ clickCount: 3 }); // select just the heading
    await page.getByTestId("tb-link").click();
    await page.getByTestId("link-input").fill("javascript:alert(1)");
    await page.getByTestId("link-apply").click();
    await expect(page.getByTestId("link-problem")).toBeVisible();
    await expect(surface(page).locator("a")).toHaveCount(0);
    await page.getByTestId("link-input").fill("https://example.com/page");
    await page.getByTestId("link-apply").click();
    await expect(surface(b.page).locator('a[href="https://example.com/page"]')).toHaveCount(1);

    // the toolbar's Undo takes the link back
    await page.getByTestId("tb-undo").click();
    await expect(surface(b.page).locator('a[href="https://example.com/page"]')).toHaveCount(0);
  });

  test("undo reverts only my own typing, never a collaborator's", async ({ browser }) => {
    const { a, b } = await setup(browser);
    await surface(a.page).click();
    await a.page.keyboard.type("AAA");
    await expect.poll(() => textOf(b.page)).toContain("AAA");
    await a.page.waitForTimeout(700); // a pause starts a new undo step
    await surface(b.page).click();
    await b.page.keyboard.press("End");
    await b.page.keyboard.type("BBB");
    await expect.poll(() => textOf(a.page)).toContain("AAABBB");
    await surface(a.page).click();
    await a.page.keyboard.press("Control+Z");
    await expect.poll(() => textOf(a.page)).toBe("BBB");
    await expect.poll(() => textOf(b.page)).toBe("BBB");
  });

  test("an image goes through the asset endpoint; only its id is in the document; pasted pictures are refused", async ({ browser, browserName }) => {
    const { a, b } = await setup(browser);
    const png = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAGCAIAAABxZ0isAAAAEklEQVR4nGP4z8Dwn4EIwDiqEACYuhHxNrZbVAAAAABJRU5ErkJggg==",
      "base64",
    );
    await surface(a.page).click();
    await a.page.getByTestId("image-file").setInputFiles({ name: "dot.png", mimeType: "image/png", buffer: png });
    const img = surface(a.page).locator("img[data-asset-id]");
    await expect(img).toHaveCount(1);
    expect(await img.getAttribute("src")).toMatch(/^\/rooms\/[0-9a-f-]+\/assets\/[0-9a-f-]+$/);
    await expect(surface(b.page).locator("img[data-asset-id]")).toHaveCount(1);
    await img.click();
    await a.page.getByTestId("alt-input").fill("A tiny test picture");
    await a.page.getByTestId("alt-input").press("Enter");
    await expect(surface(b.page).locator("img[data-asset-id]")).toHaveAttribute("alt", "A tiny test picture");

    // pasted remote / base64 / forged-asset pictures never reach the document. Firefox ignores the
    // clipboardData of a synthetic paste event, so the browser-level paste is checked in Chromium; the
    // sanitising function itself is unit-tested for every engine in test/paste.test.ts.
    test.skip(browserName === "firefox", "synthetic paste events carry no clipboard data in Firefox");
    await surface(a.page).click();
    await a.page.evaluate(() => {
      const data = new DataTransfer();
      data.setData(
        "text/html",
        '<p>pasted</p><img src="https://evil.example/x.png"><img src="data:image/png;base64,AAAA"><img data-asset-id="11111111-1111-4111-8111-111111111111" src="/x">',
      );
      const el = document.querySelector(".tf-editor")!;
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    });
    await expect(surface(a.page)).toContainText("pasted");
    await expect(surface(a.page).locator("img[data-asset-id]")).toHaveCount(1); // still only the uploaded one
    await expect(surface(a.page).locator('img[src^="http"], img[src^="data:"]')).toHaveCount(0);
  });

  test("offline: the indicator says so and counts unsynced changes; reconnecting delivers them", async ({ browser }) => {
    const { a, b } = await setup(browser);
    const net = await controlCollab(b.page);
    await b.page.reload();
    await expect(status(b.page)).toHaveText("Synced");
    await surface(b.page).click();
    await b.page.keyboard.type("before");
    await expect.poll(() => textOf(a.page)).toContain("before");
    net.offline(true);
    await expect(status(b.page)).toContainText("Offline", { timeout: 20_000 });
    await b.page.keyboard.type(" while offline");
    await expect(status(b.page)).toContainText(/Offline \(\d+ unsynced changes?\)/);
    await expect(b.page.getByTestId("at-risk")).toBeVisible();
    expect(await textOf(a.page)).not.toContain("while offline"); // nobody else has it yet
    net.offline(false);
    await expect(status(b.page)).toHaveText("Synced", { timeout: 60_000 });
    await expect.poll(() => textOf(a.page)).toContain("before while offline");
  });

  test("mobile browsers view the document live but cannot edit it", async ({ browser }) => {
    const { roomId } = await setup(browser);
    const user = seed("Mo", { roomId, role: "editor" });
    const context = await browser.newContext({ viewport: { width: 700, height: 900 }, hasTouch: true, isMobile: true });
    await context.addCookies([{ name: user.cookie_name, value: user.secret, url: "http://localhost:5173" }]);
    const page = await context.newPage();
    await page.goto(`http://localhost:5173${roomUrl(roomId)}`);
    await expect(surface(page)).toBeVisible();
    await expect(page.getByTestId("mobile-note")).toBeVisible();
    await expect(surface(page)).toHaveAttribute("contenteditable", "false");
    await expect(page.getByRole("toolbar")).toHaveCount(0);
  });

  test("the room switching: with nothing unsynced it just follows; with unsynced changes it asks", async ({ browser }) => {
    const { b, roomId, asOwner } = await setup(browser);
    // let Eddie's collaboration socket through, but be able to stop his messages reaching the server
    const net = await controlCollab(b.page);
    await b.page.reload();
    await expect(status(b.page)).toHaveText("Synced");

    // nothing unsynced: Eddie follows the room by himself
    await asOwner.createDocument(roomId, "Second doc");
    await expect(b.page.getByRole("heading", { name: "Second doc" })).toBeVisible();
    await expect(status(b.page)).toHaveText("Synced");

    // now his edits stop reaching the server: they are "not on the server yet"
    net.blockUp(true);
    await surface(b.page).click();
    await b.page.keyboard.type("only in this tab");
    await expect(b.page.getByTestId("at-risk")).toBeVisible();
    await expect(status(b.page)).toHaveText("Syncing…");
    await asOwner.createDocument(roomId, "Third doc"); // the room switches again
    const banner = b.page.getByTestId("switch-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("The room switched to a different document");
    await expect(banner).toContainText("not synced yet");
    await expect(b.page.getByRole("heading", { name: "Second doc" })).toBeVisible(); // still here
    await b.page.getByTestId("stay").click();
    await expect(banner).toHaveCount(0);
    await expect(surface(b.page)).toContainText("only in this tab"); // nothing was thrown away
    // leaving the page now would warn: the browser asks before closing a tab with changes at risk
    const beforeUnload = await b.page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    });
    expect(beforeUnload).toBe(true);
    // "Switch anyway" is available and explicit
    await asOwner.createDocument(roomId, "Fourth doc");
    await expect(banner).toBeVisible();
    await b.page.getByTestId("switch-anyway").click();
    await expect(b.page.getByRole("heading", { name: "Fourth doc" })).toBeVisible();
  });

  test("you can switch away on purpose: a confirmation names what would be lost", async ({ browser }) => {
    const { b, roomId, asOwner } = await setup(browser);
    await asOwner.createDocument(roomId, "Other doc");
    await expect(b.page.getByRole("heading", { name: "Other doc" })).toBeVisible();
    await surface(b.page).click();
    await b.page.keyboard.type("x");
    await expect(status(b.page)).toHaveText("Synced");
    // nothing at risk: navigating away needs no confirmation
    await b.page.getByRole("button", { name: "Shared plan" }).click();
    await expect(b.page.getByRole("heading", { name: "Shared plan" })).toBeVisible();
  });

  test("a terminal close (4403) keeps the text, says Access lost and does not reconnect", async ({ browser }) => {
    const { b } = await setup(browser);
    const net = await controlCollab(b.page);
    await b.page.reload();
    await expect(status(b.page)).toHaveText("Synced");
    await surface(b.page).click();
    await b.page.keyboard.type("keep this text");
    await expect(status(b.page)).toHaveText("Synced");
    net.refuseNew(true); // anything that tries again is refused, and counted
    const before = net.attempts;
    net.closeWith(4403);
    await expect(status(b.page)).toHaveText("Access lost");
    await expect(b.page.getByTestId("access-lost")).toContainText("You no longer have access to this document");
    await expect(surface(b.page)).toContainText("keep this text");
    await expect(surface(b.page)).toHaveAttribute("contenteditable", "false");
    await b.page.waitForTimeout(6_000);
    expect(net.attempts).toBe(before); // terminal: no reconnect attempts at all
  });

  test("a temporary close (1013) reconnects by itself", async ({ browser }) => {
    const { a, b } = await setup(browser);
    const net = await controlCollab(b.page);
    await b.page.reload();
    await expect(status(b.page)).toHaveText("Synced");
    const before = net.attempts;
    net.closeWith(1013);
    await expect.poll(() => net.attempts, { timeout: 30_000 }).toBeGreaterThan(before);
    await expect(status(b.page)).toHaveText("Synced", { timeout: 30_000 });
    await surface(a.page).click();
    await a.page.keyboard.type("after the retry");
    await expect.poll(() => textOf(b.page)).toContain("after the retry");
  });

  test("a demoted editor is locked by the server while the page is open, and told", async ({ browser }) => {
    const { a, b, roomId, asOwner } = await setup(browser);
    const editorId = (await asOwner.get<{ items: { user_id: string; display_name: string }[] }>(`/rooms/${roomId}/members`)).items.find((m) => m.display_name === "Eddie")!.user_id;
    const changed = await asOwner.send("PATCH", `/rooms/${roomId}/members/${editorId}`, { role: "viewer" });
    expect(changed.status).toBe(200);
    await expect(b.page.getByTestId("demoted")).toBeVisible({ timeout: 20_000 });
    await expect(status(b.page)).toHaveText("Read-only");
    await expect(surface(b.page)).toHaveAttribute("contenteditable", "false");
    // the owner keeps editing and the demoted user still sees it live
    await surface(a.page).click();
    await a.page.keyboard.type("owner continues");
    await expect.poll(() => textOf(b.page)).toContain("owner continues");
    await b.page.getByTestId("reload-doc").click();
    await expect(surface(b.page)).toContainText("owner continues");
  });
});
