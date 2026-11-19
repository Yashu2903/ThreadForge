import { type Page, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The room rail (the strip of room icons) and the layout around it: navigation between rooms, what happens to the
 * live connections when the room changes, keyboard access, and the narrow-screen layout.
 */
const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const rail = (page: Page) => page.getByRole("navigation", { name: "Your rooms" });

/** the app's own WebSockets (the room channel and the documents), so a test can tell which are still open.
 *  The Vite dev server's hot-reload socket is not the app's and stays open by design. */
function trackSockets(page: Page) {
  const sockets: { url: string; closed: () => boolean }[] = [];
  page.on("websocket", (ws) => {
    if (/\/rooms\/[^/]+\/ws|\/collab/.test(ws.url())) sockets.push({ url: ws.url(), closed: () => ws.isClosed() });
  });
  return { open: () => sockets.filter((s) => !s.closed()), all: sockets };
}

async function twoRooms(browser: import("@playwright/test").Browser) {
  const user = seed("Rita Rail");
  const as = await AsUser.create(user);
  const roomA = await as.createRoom("Alpha team");
  const roomB = await as.createRoom("Beta crew");
  await as.createDocument(roomA, "Alpha plan");
  await as.createDocument(roomB, "Beta notes");
  const { page, context } = await signedIn(browser, user);
  return { as, page, context, roomA, roomB };
}

test.describe("room rail and layout", () => {
  test("lists the rooms, marks the current one, and switching rooms closes the old room's connections", async ({ browser }) => {
    test.setTimeout(120_000);
    const { page, roomA, roomB } = await twoRooms(browser);
    const sockets = trackSockets(page);

    await page.goto(roomUrl(roomA));
    await expect(page.getByRole("heading", { name: "Alpha plan", exact: true })).toBeVisible();
    await expect(surface(page)).toBeVisible();
    await expect(page.getByTestId("sync-status")).toHaveText("Synced");
    await expect(rail(page).getByRole("link", { name: "Alpha team" })).toHaveAttribute("aria-current", "page");
    await expect(rail(page).getByRole("link", { name: "Beta crew" })).not.toHaveAttribute("aria-current", "page");
    await surface(page).click();
    await page.keyboard.type("Alpha text that stays.");
    await expect(page.getByTestId("sync-status")).toHaveText("Synced");
    const openInA = sockets.open(); // these exact connections (both rooms' documents share one /collab URL, so compare objects)
    expect(openInA.length).toBeGreaterThanOrEqual(2); // the room channel and the document
    // (React StrictMode opens and closes the document socket once while mounting, so only open ones are counted)

    // switch with the rail: the old room's sockets close, new ones open, and nothing piles up
    await rail(page).getByRole("link", { name: "Beta crew" }).click();
    await expect(page.getByRole("heading", { name: "Beta notes", exact: true })).toBeVisible();
    await expect(rail(page).getByRole("link", { name: "Beta crew" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByTestId("sync-status")).toHaveText("Synced");
    await expect.poll(() => openInA.every((s) => s.closed()), { timeout: 15_000 }).toBe(true);
    await expect.poll(() => sockets.open().length, { timeout: 15_000 }).toBe(openInA.length);
    await expect(surface(page)).not.toContainText("Alpha text that stays.");

    // coming back: the document comes from the server, not from leftover page state
    await rail(page).getByRole("link", { name: "Alpha team" }).click();
    await expect(page.getByRole("heading", { name: "Alpha plan", exact: true })).toBeVisible();
    await expect(surface(page)).toContainText("Alpha text that stays.");
    await expect.poll(() => sockets.open().length, { timeout: 15_000 }).toBe(openInA.length);

    // the home button goes to the list of rooms
    await page.getByRole("button", { name: "Back to your rooms" }).click();
    await expect(page.getByRole("heading", { name: "Your rooms" })).toBeVisible();
    await expect.poll(() => sockets.open().length, { timeout: 15_000 }).toBe(0);
  });

  test("works from the keyboard and shows where focus is", async ({ browser }) => {
    test.setTimeout(90_000);
    const { page, roomA } = await twoRooms(browser);
    await page.goto(roomUrl(roomA));
    await expect(page.getByRole("heading", { name: "Alpha plan", exact: true })).toBeVisible();
    const home = page.getByRole("button", { name: "Back to your rooms" });
    await home.focus();
    const outline = await home.evaluate((el) => {
      const s = getComputedStyle(el);
      return { style: s.outlineStyle, width: parseFloat(s.outlineWidth) };
    });
    expect(outline.style).not.toBe("none");
    expect(outline.width).toBeGreaterThan(0);
    // Tab walks the room links in the order they are listed; Enter on the one that is not current opens that room
    const names = await rail(page).getByRole("link").evaluateAll((links) => links.map((l) => l.getAttribute("aria-label")));
    expect([...names].sort()).toEqual(["Alpha team", "Beta crew"]);
    const target = names.indexOf("Beta crew");
    for (let i = 0; i <= target; i += 1) {
      await page.keyboard.press("Tab");
      await expect(rail(page).getByRole("link", { name: names[i]! })).toBeFocused();
    }
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Beta notes", exact: true })).toBeVisible();
    await expect(rail(page).getByRole("link", { name: "Beta crew" })).toHaveAttribute("aria-current", "page");
  });

  for (const [width, height] of [[1600, 900], [1280, 800], [820, 900], [390, 844]] as const) {
    test(`has no sideways scrolling at ${width}px and keeps the editor, chat and formatting reachable`, async ({ browser }) => {
      test.setTimeout(90_000);
      const { page, roomA } = await twoRooms(browser);
      await page.setViewportSize({ width, height });
      await page.goto(roomUrl(roomA));
      await expect(page.getByRole("heading", { name: "Alpha plan", exact: true })).toBeVisible();
      await expect(surface(page)).toBeVisible();
      await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
      await expect(page.getByRole("toolbar", { name: "Formatting" })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await page.screenshot({ path: `test-results/room-layout-${width}.png`, fullPage: false });
    });
  }
});
