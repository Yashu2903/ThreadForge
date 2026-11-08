import { type Browser, type BrowserContext, type Page, expect, test } from "@playwright/test";
import { AsUser, type Seeded, roomUrl, seed, signedIn } from "./helpers";

/**
 * Saving, conflicts, switching and access changes in real browsers against the real stack
 * (API, outbox dispatcher, Redis, PostgreSQL, storage). Two or three signed-in users at once.
 */

const status = (page: Page) => page.getByTestId("save-status");
const editor = (page: Page) => page.getByTestId("editor");
const blockTexts = (page: Page) =>
  page.evaluate(() => {
    const doc = (window as any).__editor.getDocument();
    const text = (b: any): string => (b.children ?? []).map((c: any) => (c.type === "text" ? c.text : "")).join("");
    return doc.blocks.map(text);
  });
const PUT = /\/rooms\/[0-9a-f-]+\/documents\/[0-9a-f-]+\/content$/;

async function member(browser: Browser, user: Seeded, roomId: string) {
  const { context, page } = await signedIn(browser, user);
  await page.goto(roomUrl(roomId));
  return { context, page };
}

async function focusEnd(page: Page) {
  await editor(page).focus();
  await page.keyboard.press("Control+End");
}

/** An owner with a room that has one blank document, plus an editor and a viewer. */
async function setupRoom(browser: Browser, title = "Shared doc") {
  const owner = seed("Owner " + Math.random().toString(36).slice(2, 6));
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Room " + title);
  const doc = await asOwner.createDocument(roomId, title);
  const editorUser = seed("Editor " + Math.random().toString(36).slice(2, 6), { roomId, role: "editor" });
  const viewerUser = seed("Viewer " + Math.random().toString(36).slice(2, 6), { roomId, role: "viewer" });
  return { owner, asOwner, roomId, doc, editorUser, viewerUser, browser };
}

test.describe("saving", () => {
  test("start a blank document and a template from the UI, type and format, autosave, refresh: nothing lost", async ({ browser }) => {
    const owner = seed("Saver");
    const asOwner = await AsUser.create(owner);
    const roomId = await asOwner.createRoom("Save room");
    const { context, page } = await member(browser, owner, roomId);

    await page.getByRole("main", { name: "Document" }).getByRole("button", { name: "Start document" }).click();
    await page.getByLabel("Title").fill("My notes");
    await page.getByRole("button", { name: "Create document" }).click();
    await expect(editor(page)).toBeVisible();
    await expect(status(page)).toHaveText("Saved");

    await focusEnd(page);
    await page.keyboard.type("hello world");
    await expect(status(page)).toHaveText("Unsaved changes");
    for (let i = 0; i < 5; i++) await page.keyboard.press("Shift+ArrowLeft");
    await page.keyboard.press("Control+b");
    await expect(status(page)).toHaveText("Saved"); // autosave fired ~1 s after the edits settled
    await expect(page.getByTestId("doc-version")).toHaveText("Version 2"); // typing and bold settled together: one save

    await page.reload();
    await expect(editor(page)).toBeVisible();
    expect(await blockTexts(page)).toEqual(["hello world"]);
    await expect(editor(page).locator("strong")).toHaveText("world");
    await expect(status(page)).toHaveText("Saved");

    // a template document
    await page.getByRole("button", { name: "Start document" }).first().click();
    await page.getByLabel("Title").fill("Proposal");
    await page.getByRole("radio", { name: /Technical proposal/ }).check();
    await page.getByRole("button", { name: "Create document" }).click();
    await expect(editor(page).getByRole("heading", { name: "Architecture" })).toBeVisible();
    expect((await blockTexts(page)).slice(0, 4)).toEqual(["Problem", "", "Proposed approach", ""]);
    await context.close();
    await asOwner.dispose();
  });

  test("a viewer sees the document read-only, with no editor and no toolbar", async ({ browser }) => {
    const s = await setupRoom(browser);
    const v = await member(browser, s.viewerUser, s.roomId);
    await expect(v.page.getByTestId("document-read")).toBeVisible();
    await expect(v.page.getByTestId("access-mode")).toHaveText("Access: read-only");
    await expect(editor(v.page)).toHaveCount(0);
    await expect(v.page.getByRole("toolbar")).toHaveCount(0);
    await v.context.close();
  });

  test("typing during an in-flight save keeps the newer text; the next save uses the returned version", async ({ browser }) => {
    const s = await setupRoom(browser);
    const e = await member(browser, s.editorUser, s.roomId);
    await expect(editor(e.page)).toBeVisible();
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const seen: number[] = [];
    await e.page.route(PUT, async (route) => {
      seen.push(JSON.parse(route.request().postData() ?? "{}").expected_content_version);
      if (seen.length === 1) await gate; // hold only the first save
      await route.continue();
    });
    await focusEnd(e.page);
    await e.page.keyboard.type("first");
    await expect.poll(() => seen.length).toBe(1); // the save is now in flight (held)
    await expect(status(e.page)).toHaveText("Saving…");
    await e.page.keyboard.type(" and then more");
    release();
    await expect(status(e.page)).toHaveText("Saved");
    expect(seen).toEqual([1, 2]); // the second save used the version the first one returned
    expect(await blockTexts(e.page)).toEqual(["first and then more"]);
    await e.page.reload();
    await expect(editor(e.page)).toBeVisible();
    expect(await blockTexts(e.page)).toEqual(["first and then more"]);
    await e.context.close();
  });

  test("a failed save keeps the local text; Retry now saves it", async ({ browser }) => {
    const s = await setupRoom(browser);
    const e = await member(browser, s.editorUser, s.roomId);
    await expect(editor(e.page)).toBeVisible();
    await e.page.route(PUT, (route) => route.abort("failed"));
    await focusEnd(e.page);
    await e.page.keyboard.type("do not lose me");
    await expect(status(e.page)).toHaveText("Save failed");
    await expect(e.page.getByTestId("save-message")).toContainText("Your text is safe here");
    expect(await blockTexts(e.page)).toEqual(["do not lose me"]);
    await e.page.unroute(PUT);
    await e.page.getByTestId("retry-button").click();
    await expect(status(e.page)).toHaveText("Saved");
    await e.context.close();
  });

  test("the page asks the browser to warn before closing while anything is unsaved, and only then", async ({ browser }) => {
    const s = await setupRoom(browser);
    const e = await member(browser, s.editorUser, s.roomId);
    await expect(editor(e.page)).toBeVisible();
    const warns = () =>
      e.page.evaluate(() => {
        const event = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(event);
        return event.defaultPrevented;
      });
    expect(await warns()).toBe(false); // nothing unsaved: leaving is silent
    await e.page.route(PUT, (route) => route.abort("failed"));
    await focusEnd(e.page);
    await e.page.keyboard.type("unsaved");
    await expect(status(e.page)).toHaveText("Save failed");
    expect(await warns()).toBe(true);
    await e.page.unroute(PUT);
    await e.page.getByTestId("retry-button").click();
    await expect(status(e.page)).toHaveText("Saved");
    expect(await warns()).toBe(false);
    await e.context.close();
  });
});

test.describe("two sessions", () => {
  test("a clean copy follows another session's save; a dirty copy gets a visible conflict from the EVENT, before any 409", async ({ browser }) => {
    const s = await setupRoom(browser);
    const a = await member(browser, s.editorUser, s.roomId);
    const b = await member(browser, s.owner, s.roomId);
    await expect(editor(a.page)).toBeVisible();
    await expect(editor(b.page)).toBeVisible();

    // A saves; B (clean) follows without any action
    await focusEnd(a.page);
    await a.page.keyboard.type("from A");
    await expect(status(a.page)).toHaveText("Saved");
    await expect.poll(() => blockTexts(b.page)).toEqual(["from A"]);
    await expect(status(b.page)).toHaveText("Saved");

    // B goes offline-ish: every save attempt fails, so B holds unsaved work and never reaches the server
    const attempts: string[] = [];
    await b.page.route(PUT, (route) => {
      attempts.push("attempt");
      return route.abort("failed");
    });
    await focusEnd(b.page);
    await b.page.keyboard.type(" plus B");
    await expect(status(b.page)).toHaveText("Save failed");

    // A saves again: the realtime event reaches B, which holds unsaved work -> conflict, no 409 involved
    await focusEnd(a.page);
    await a.page.keyboard.type(" and A again");
    await expect(status(a.page)).toHaveText("Saved");
    await expect(b.page.getByTestId("conflict")).toBeVisible();
    await expect(b.page.getByTestId("conflict")).toContainText("Another session saved a newer version");
    await expect(status(b.page)).toHaveText("Conflict");
    expect(await blockTexts(b.page)).toEqual(["from A plus B"]); // B's text was NOT replaced
    await b.page.unroute(PUT);
    await b.page.waitForTimeout(2500); // nothing is saved while in conflict, even after the network returns
    await expect(status(b.page)).toHaveText("Conflict");

    // recovery: copy my text, then Load latest after confirming
    await b.context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await b.page.getByTestId("copy-text").click();
    await expect(b.page.getByTestId("copied")).toHaveText("Copied to the clipboard.");
    expect(await b.page.evaluate(() => navigator.clipboard.readText())).toBe("from A plus B");
    await b.page.getByTestId("load-latest").click();
    await expect(b.page.getByRole("dialog", { name: "Load the latest version?" })).toContainText("Your unsaved changes will be discarded");
    await b.page.getByTestId("confirm-load-latest").click();
    await expect(status(b.page)).toHaveText("Saved");
    expect(await blockTexts(b.page)).toEqual(["from A and A again"]);
    // B can edit and save again from the new base
    await focusEnd(b.page);
    await b.page.keyboard.type("!");
    await expect(status(b.page)).toHaveText("Saved");
    await expect.poll(() => blockTexts(a.page)).toEqual(["from A and A again!"]);
    await a.context.close();
    await b.context.close();
  });

  test("simultaneous edits: the loser gets a conflict from the server's 409 and keeps its text", async ({ browser }) => {
    const s = await setupRoom(browser);
    const a = await member(browser, s.editorUser, s.roomId);
    const b = await member(browser, s.owner, s.roomId);
    await expect(editor(a.page)).toBeVisible();
    await expect(editor(b.page)).toBeVisible();
    // hold B's request until A has saved, so both are based on version 1
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    await b.page.route(PUT, async (route) => {
      await gate;
      await route.continue();
    });
    await focusEnd(b.page);
    await b.page.keyboard.type("B wrote this");
    await expect(status(b.page)).toHaveText("Saving…");
    await focusEnd(a.page);
    await a.page.keyboard.type("A wrote this");
    await expect(status(a.page)).toHaveText("Saved");
    release();
    await expect(b.page.getByTestId("conflict")).toBeVisible();
    expect(await blockTexts(b.page)).toEqual(["B wrote this"]);
    await a.context.close();
    await b.context.close();
  });

  test("the room switching documents: a clean viewer follows, a client with unsaved work is asked", async ({ browser }) => {
    const s = await setupRoom(browser, "First");
    const clean = await member(browser, s.viewerUser, s.roomId);
    const busy = await member(browser, s.editorUser, s.roomId);
    await expect(editor(busy.page)).toBeVisible();
    await expect(clean.page.getByRole("heading", { name: "First", exact: true })).toBeVisible();

    await busy.page.route(PUT, (route) => route.abort("failed")); // keeps the editor's work unsaved
    await focusEnd(busy.page);
    await busy.page.keyboard.type("my unsaved work");
    await expect(status(busy.page)).toHaveText("Save failed");

    await s.asOwner.createDocument(s.roomId, "Second", "meeting_notes"); // becomes the active document

    await expect(clean.page.getByRole("heading", { name: "Second", exact: true })).toBeVisible(); // followed automatically
    await expect(clean.page.getByTestId("document-read").getByRole("heading", { name: "Agenda" })).toBeVisible();

    await expect(busy.page.getByTestId("switch-banner")).toBeVisible();
    expect(await blockTexts(busy.page)).toEqual(["my unsaved work"]); // never replaced
    await busy.page.getByTestId("discard-switch").click();
    await expect(busy.page.getByRole("heading", { name: "Second", exact: true })).toBeVisible();
    await expect(busy.page.getByTestId("switch-banner")).toHaveCount(0);
    await clean.context.close();
    await busy.context.close();
  });

  test("Save, then switch saves first and then follows the room", async ({ browser }) => {
    const s = await setupRoom(browser, "Alpha");
    const e = await member(browser, s.editorUser, s.roomId);
    await expect(editor(e.page)).toBeVisible();
    await e.page.route(PUT, (route) => route.abort("failed"));
    await focusEnd(e.page);
    await e.page.keyboard.type("keep this");
    await expect(status(e.page)).toHaveText("Save failed");
    await s.asOwner.createDocument(s.roomId, "Beta");
    await expect(e.page.getByTestId("switch-banner")).toBeVisible();
    await e.page.unroute(PUT);
    await e.page.getByTestId("save-then-switch").click();
    await expect(e.page.getByRole("heading", { name: "Beta", exact: true })).toBeVisible();
    const first = await s.asOwner.get(`/rooms/${s.roomId}/documents/${s.doc.id}`);
    expect(first.content.blocks[0].children[0].text).toBe("keep this"); // it really was saved
    await e.context.close();
  });
});

test.describe("losing access", () => {
  test("a role downgrade stops autosave and locks the editor but keeps the unsaved text", async ({ browser }) => {
    const s = await setupRoom(browser);
    const e = await member(browser, s.editorUser, s.roomId);
    await expect(editor(e.page)).toBeVisible();
    let puts = 0;
    await e.page.route(PUT, (route) => {
      puts++;
      return route.abort("failed");
    });
    await focusEnd(e.page);
    await e.page.keyboard.type("typed before downgrade");
    await expect(status(e.page)).toHaveText("Save failed");
    const before = puts;

    const res = await s.asOwner.send("PATCH", `/rooms/${s.roomId}/members/${s.editorUser.user_id}`, { role: "viewer" });
    expect(res.status).toBe(200);
    await expect(status(e.page)).toHaveText("Read-only");
    await expect(editor(e.page)).toHaveAttribute("contenteditable", "false");
    await expect(e.page.getByTestId("readonly-unsaved")).toBeVisible();
    expect(await blockTexts(e.page)).toEqual(["typed before downgrade"]);
    await e.page.waitForTimeout(3000);
    expect(puts).toBe(before); // no further saves
    await expect(e.page.getByRole("toolbar")).toHaveCount(0);
    await e.context.close();
  });

  test("removal shows the access-lost screen with a way to copy the unsaved text", async ({ browser }) => {
    const s = await setupRoom(browser);
    const e = await member(browser, s.editorUser, s.roomId);
    await e.context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await expect(editor(e.page)).toBeVisible();
    await e.page.route(PUT, (route) => route.abort("failed"));
    await focusEnd(e.page);
    await e.page.keyboard.type("words I cannot lose");
    await expect(status(e.page)).toHaveText("Save failed");
    const res = await s.asOwner.send("DELETE", `/rooms/${s.roomId}/members/${s.editorUser.user_id}`, undefined);
    expect(res.status).toBe(204);
    await expect(e.page.getByRole("heading", { name: "You no longer have access to this room" })).toBeVisible();
    await expect(e.page.getByTestId("access-lost-unsaved")).toBeVisible();
    await e.page.getByRole("button", { name: "Copy my unsaved text" }).click();
    expect(await e.page.evaluate(() => navigator.clipboard.readText())).toBe("words I cannot lose");
    await e.context.close();
  });
});

test.describe("beside the editor", () => {
  test("chat keeps working while editing, and editing is not disturbed by incoming messages", async ({ browser }) => {
    const s = await setupRoom(browser);
    const e = await member(browser, s.editorUser, s.roomId);
    const o = await member(browser, s.owner, s.roomId);
    await expect(editor(e.page)).toBeVisible();
    await focusEnd(e.page);
    await e.page.keyboard.type("typing in the document");
    await o.page.getByRole("textbox", { name: "Message" }).fill("hello editor");
    await o.page.getByRole("button", { name: "Send" }).click();
    await expect(e.page.getByRole("log", { name: "Messages" })).toContainText("hello editor");
    await e.page.keyboard.type(" - still typing");
    expect(await blockTexts(e.page)).toEqual(["typing in the document - still typing"]);
    await e.page.getByRole("textbox", { name: "Message" }).fill("reply from the editor");
    await e.page.getByRole("button", { name: "Send" }).click();
    await expect(o.page.getByRole("log", { name: "Messages" })).toContainText("reply from the editor");
    await expect(status(e.page)).toHaveText("Saved");
    await e.context.close();
    await o.context.close();
  });

  test("a mobile browser can view but not edit", async ({ browser }) => {
    const s = await setupRoom(browser);
    const context: BrowserContext = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, baseURL: "http://localhost:5173" });
    await context.addCookies([{ name: s.editorUser.cookie_name, value: s.editorUser.secret, url: "http://localhost:5173" }]);
    const page = await context.newPage();
    await page.goto(roomUrl(s.roomId));
    await expect(page.getByTestId("mobile-note")).toContainText("not supported in mobile browsers");
    await expect(page.getByTestId("document-read")).toBeVisible();
    await expect(editor(page)).toHaveCount(0);
    await context.close();
  });
});
