import { type Page, expect, test } from "@playwright/test";

/** Formatting, lists, links and tables in real browsers, driven through the toolbar and the keyboard. */

async function open(page: Page) {
  const warnings: string[] = [];
  page.on("console", (m) => m.text().includes("[editor]") && warnings.push(m.text()));
  await page.goto("/#/playground");
  await expect(page.getByTestId("editor")).toBeVisible();
  await page.getByTestId("editor").focus();
  await page.keyboard.press("Control+End");
  return warnings;
}
const getDoc = (page: Page) => page.evaluate(() => (window as any).__editor.getDocument());
const lastBlock = async (page: Page) => (await getDoc(page)).blocks.at(-1);
const noDrift = (page: Page) => page.evaluate(() => (window as any).__editor.findDrift());
const bar = (page: Page) => page.getByRole("toolbar", { name: "Formatting" });
const tb = (page: Page, name: string) => bar(page).getByRole("button", { name, exact: true });

async function selectLeft(page: Page, n: number) {
  for (let i = 0; i < n; i++) await page.keyboard.press("Shift+ArrowLeft");
}

test.describe("formatting", () => {
  test("bold from the toolbar keeps the selection; the button reflects the caret; Ctrl+B toggles", async ({ page }) => {
    const warnings = await open(page);
    await page.keyboard.type("hello world");
    await selectLeft(page, 5);
    await tb(page, "Bold").click();
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "hello " }, { type: "text", text: "world", marks: { bold: true } }]);
    // the click did not steal the selection: typing replaces "world"
    await page.keyboard.type("there");
    expect((await lastBlock(page)).children[1]).toEqual({ type: "text", text: "there", marks: { bold: true } });
    await expect(tb(page, "Bold")).toHaveAttribute("aria-pressed", "true"); // caret is inside bold text
    await page.keyboard.press("Home");
    await expect(tb(page, "Bold")).toHaveAttribute("aria-pressed", "false");
    await page.keyboard.press("Shift+End");
    await page.keyboard.press("Control+b"); // mixed selection: turns ON everywhere
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "hello there", marks: { bold: true } }]);
    await page.keyboard.press("Control+b");
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "hello there" }]);
    expect(await noDrift(page)).toEqual([]);
    expect(warnings).toEqual([]);
  });

  test("typing-state marks: toggling bold at a caret applies to the next text, not the document", async ({ page }) => {
    await open(page);
    await page.keyboard.press("Enter");
    const before = await getDoc(page);
    await page.keyboard.press("Control+b");
    expect(await getDoc(page)).toEqual(before); // nothing stored yet
    await expect(tb(page, "Bold")).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.type("bold");
    await page.keyboard.press("Control+b");
    await page.keyboard.type(" plain");
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "bold", marks: { bold: true } }, { type: "text", text: " plain" }]);
  });

  test("font, size, colour, highlight, script", async ({ page }) => {
    await open(page);
    await page.keyboard.type("styled");
    await page.keyboard.press("Shift+Home");
    await page.getByTestId("tb-font").selectOption("calibri");
    await page.getByTestId("tb-size").selectOption("14");
    await page.getByTestId("tb-color").fill("#ff0000");
    await page.getByTestId("tb-highlight").fill("#ffff00");
    await tb(page, "Superscript").click();
    expect((await lastBlock(page)).children).toEqual([
      { type: "text", text: "styled", marks: { font: "calibri", size_pt: 14, color: "#FF0000", highlight: "#FFFF00", script: "superscript" } },
    ]);
    await expect(page.getByTestId("tb-font")).toHaveValue("calibri");
    await tb(page, "Superscript").click(); // toggles off
    await tb(page, "Subscript").click();
    expect((await lastBlock(page)).children[0].marks.script).toBe("subscript");
    expect(await page.getByTestId("editor").locator("span.font-calibri").count()).toBe(1);
  });

  test("text style, alignment, line spacing, spacing and indent", async ({ page }) => {
    await open(page);
    await page.keyboard.type("Heading text");
    await page.getByTestId("tb-block").selectOption("h2");
    await tb(page, "Align center").click();
    await page.getByTestId("tb-line-spacing").selectOption("1.5");
    await page.getByTestId("tb-space-after").selectOption("12");
    await tb(page, "Increase indent").click();
    const block = await lastBlock(page);
    expect(block).toMatchObject({ type: "heading", level: 2, attrs: { align: "center", line_spacing: 1.5, space_after_pt: 12, indent_level: 1 } });
    await expect(tb(page, "Align center")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("tb-block")).toHaveValue("h2");
    await page.getByTestId("tb-block").selectOption("paragraph");
    expect((await lastBlock(page)).type).toBe("paragraph");
    await tb(page, "Align left").click();
    await tb(page, "Decrease indent").click();
    expect((await lastBlock(page)).attrs).toEqual({ line_spacing: 1.5, space_after_pt: 12 });
  });

  test("a mixed selection shows (mixed) and applies a value to everything", async ({ page }) => {
    await open(page);
    await page.keyboard.type("aaa bbb");
    await page.keyboard.press("Home");
    await page.keyboard.press("Control+ArrowRight");
    await page.keyboard.press("Shift+End");
    await page.getByTestId("tb-size").selectOption("18");
    await page.keyboard.press("Control+a");
    await expect(page.getByTestId("tb-size")).toHaveValue("mixed");
    await page.getByTestId("tb-size").selectOption("12");
    const sizes = (await lastBlock(page)).children.map((c: any) => c.marks?.size_pt);
    expect(sizes).toEqual([12]);
  });
});

test.describe("lists", () => {
  test("bullets from the toolbar; Enter, Tab, Shift+Tab, and Enter in an empty item", async ({ page }) => {
    await open(page);
    await page.keyboard.type("one");
    await tb(page, "Bulleted list").click();
    await page.keyboard.press("Enter");
    await page.keyboard.type("two");
    await page.keyboard.press("Tab"); // indent under "one"
    await page.keyboard.press("Enter");
    await page.keyboard.type("three");
    await page.keyboard.press("Shift+Tab"); // back to level 1
    let list = (await getDoc(page)).blocks.at(-1);
    expect(list.type).toBe("list");
    expect(list.children).toHaveLength(2); // one(>two), three
    expect(list.children[0].children.map((c: any) => c.type)).toEqual(["paragraph", "list"]);
    await page.keyboard.press("Enter"); // new empty item
    await page.keyboard.press("Enter"); // empty item: leaves the list
    const blocks = (await getDoc(page)).blocks;
    expect(blocks.at(-1).type).toBe("paragraph");
    expect(blocks.at(-2).type).toBe("list");
    expect(await page.getByTestId("editor").locator("ul ul").count()).toBe(1);
    await page.keyboard.type("after");
    await page.keyboard.press("Control+z");
    await page.keyboard.press("Control+z");
    expect(await noDrift(page)).toEqual([]);
  });

  test("numbered lists, and Backspace at the start of an item removes the bullet", async ({ page }) => {
    await open(page);
    await page.keyboard.type("first");
    await tb(page, "Numbered list").click();
    expect(await page.getByTestId("editor").locator("ol li").count()).toBe(1);
    await expect(tb(page, "Numbered list")).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.press("Home");
    await page.keyboard.press("Backspace");
    expect((await lastBlock(page)).type).toBe("paragraph");
    expect(await page.getByTestId("editor").locator("ol").count()).toBe(0);
  });

  test("Tab in a plain paragraph leaves the editor", async ({ page }) => {
    await open(page);
    await page.keyboard.type("plain");
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => document.activeElement?.closest("[data-testid=editor]") !== null)).toBe(false);
  });
});

test.describe("links", () => {
  test("Ctrl+K opens the link form; apply, edit, remove; unsafe addresses are refused", async ({ page }) => {
    await open(page);
    await page.keyboard.type("see docs");
    await selectLeft(page, 4);
    await page.keyboard.press("Control+k");
    await page.getByTestId("link-input").fill("javascript:alert(1)");
    await page.getByTestId("link-apply").click();
    await expect(page.getByTestId("notice")).toContainText("http://, https:// or mailto:");
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "see docs" }]);
    await page.getByTestId("link-input").fill("https://example.com/docs");
    await page.getByTestId("link-apply").click();
    expect((await lastBlock(page)).children[1]).toEqual({ type: "text", text: "docs", marks: { link: { href: "https://example.com/docs" } } });
    const a = page.getByTestId("editor").locator("a");
    await expect(a).toHaveAttribute("rel", "noopener noreferrer");
    await expect(a).toHaveAttribute("target", "_blank");
    // caret inside the link: the form shows its address, "Remove link" appears
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("Control+k");
    await expect(page.getByTestId("link-input")).toHaveValue("https://example.com/docs");
    await page.getByTestId("link-remove").click();
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "see docs" }]);
  });

  test("a plain click does not follow a link; Ctrl-click opens it in a new tab", async ({ page, context }) => {
    await open(page);
    await page.keyboard.type("go");
    await selectLeft(page, 2);
    await page.keyboard.press("Control+k");
    await page.getByTestId("link-input").fill("https://example.com/");
    await page.getByTestId("link-apply").click();
    const a = page.getByTestId("editor").locator("a");
    await a.click();
    expect(new URL(page.url()).pathname).toBe("/");
    expect(page.url()).toContain("playground");
    const popup = context.waitForEvent("page");
    await a.click({ modifiers: ["Control"] });
    const opened = await popup;
    expect(opened.url()).toContain("example.com");
    await opened.close();
  });
});

test.describe("tables", () => {
  test("insert, Tab through cells, Tab in the last cell adds a row, row/column operations, delete", async ({ page }) => {
    await open(page);
    await page.keyboard.type("before");
    await tb(page, "Insert table").click();
    await page.getByTestId("table-rows").fill("2");
    await page.getByTestId("table-cols").fill("2");
    await page.getByTestId("table-insert").click();
    const table = page.getByTestId("editor").locator("table");
    await expect(table.locator("tr")).toHaveCount(2);
    await expect(table.locator("th")).toHaveCount(2); // header row
    await page.keyboard.type("a");
    await page.keyboard.press("Tab");
    await page.keyboard.type("b");
    await page.keyboard.press("Tab");
    await page.keyboard.type("c");
    await page.keyboard.press("Tab");
    await page.keyboard.type("d");
    await page.keyboard.press("Tab"); // last cell: new row
    await expect(table.locator("tr")).toHaveCount(3);
    await page.keyboard.type("e");
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.type("!");
    const cells = await table.locator("td, th").allInnerTexts();
    expect(cells.slice(0, 5)).toEqual(["a", "b", "!c", "d", "e"]); // Shift+Tab twice from "e" lands in "c"
    await tb(page, "Add column right").click();
    await expect(table.locator("tr").first().locator("th, td")).toHaveCount(3);
    await tb(page, "Add row above").click();
    await expect(table.locator("tr")).toHaveCount(4);
    await tb(page, "Delete column").click();
    await tb(page, "Delete row").click();
    await expect(table.locator("tr")).toHaveCount(3);
    await tb(page, "Delete table").click();
    await expect(page.getByTestId("editor").locator("table")).toHaveCount(0);
    expect(await noDrift(page)).toEqual([]);
  });

  test("typing across two cells is refused with a visible hint; marks still apply", async ({ page }) => {
    await open(page);
    await tb(page, "Insert table").click();
    await page.getByTestId("table-rows").fill("1");
    await page.getByTestId("table-cols").fill("2");
    await page.getByTestId("table-insert").click();
    await page.keyboard.type("ab");
    await page.keyboard.press("Tab");
    await page.keyboard.type("cd");
    await page.keyboard.press("Shift+ArrowLeft");
    await page.keyboard.press("Shift+ArrowLeft");
    await page.keyboard.press("Shift+ArrowLeft");
    await page.keyboard.press("Shift+ArrowLeft");
    const before = await getDoc(page);
    await page.keyboard.type("X");
    await expect(page.getByTestId("notice")).toContainText("spans table cells");
    expect(await getDoc(page)).toEqual(before);
    await page.keyboard.press("Control+b");
    expect(JSON.stringify(await getDoc(page))).toContain('"bold":true');
  });
});

test.describe("toolbar accessibility", () => {
  test("one tab stop, arrow keys roam, every control is named, toggles expose aria-pressed", async ({ page }) => {
    await open(page);
    const toolbar = bar(page);
    await expect(toolbar).toHaveAttribute("aria-label", "Formatting");
    const items = toolbar.locator("[data-toolbar-item]");
    const count = await items.count();
    expect(count).toBeGreaterThan(20);
    for (let i = 0; i < count; i++) {
      const name = (await items.nth(i).getAttribute("aria-label")) ?? "";
      expect(name.length, `control ${i} needs an accessible name`).toBeGreaterThan(0);
    }
    const tabStops = await toolbar.locator('[data-toolbar-item][tabindex="0"]').count();
    expect(tabStops).toBe(1);
    await toolbar.locator('[data-toolbar-item][tabindex="0"]').focus();
    await page.keyboard.press("ArrowRight");
    const focused = await page.evaluate(() => document.activeElement?.getAttribute("aria-label"));
    expect(focused).toBe("Redo");
    await page.keyboard.press("End");
    expect(await page.evaluate(() => document.activeElement?.closest("[role=toolbar]") !== null)).toBe(true);
    await page.keyboard.press("Tab"); // leaves the toolbar
    expect(await page.evaluate(() => document.activeElement?.closest("[role=toolbar]") !== null)).toBe(false);
    for (const name of ["Bold", "Italic", "Underline", "Strikethrough", "Superscript", "Subscript", "Quote", "Bulleted list", "Numbered list"]) {
      expect(await tb(page, name).getAttribute("aria-pressed")).not.toBeNull();
    }
    await expect(tb(page, "Undo")).toHaveAttribute("aria-disabled", "true");
    await expect(tb(page, "Bold")).toHaveAttribute("title", /Bold \((Ctrl|Cmd)\+B\)/);
    await expect(page.getByTestId("editor")).toHaveAttribute("aria-multiline", "true");
  });

  test("fonts are served from our own origin (no third-party font request)", async ({ page }) => {
    const external: string[] = [];
    page.on("request", (r) => {
      if (r.resourceType() === "font" && new URL(r.url()).origin !== new URL(page.url()).origin) external.push(r.url());
    });
    await open(page);
    await page.keyboard.type("calibri text");
    await page.keyboard.press("Shift+Home");
    await page.getByTestId("tb-font").selectOption("calibri");
    const res = await page.request.get("/fonts/carlito-latin-400-normal.woff2");
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toMatch(/font|woff|octet/);
    expect((await page.request.get("/fonts/LICENSE-carlito.txt")).status()).toBe(200);
    await page.waitForFunction(() => document.fonts.check("12pt Carlito"));
    expect(external).toEqual([]);
  });
});
