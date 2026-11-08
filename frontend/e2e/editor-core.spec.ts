import { type Page, expect, test } from "@playwright/test";

/**
 * The editor core in real browsers, on the development playground (#/playground). Real key events
 * go through the browser's own input pipeline into beforeinput, so this exercises what jsdom cannot.
 */

const START = "a0000000-0000-4000-8000-000000000001";

async function open(page: Page) {
  const warnings: string[] = [];
  page.on("console", (m) => {
    if (m.text().includes("[editor]")) warnings.push(m.text());
  });
  await page.goto("/#/playground");
  await expect(page.getByTestId("editor")).toBeVisible();
  return warnings;
}

const blocks = (page: Page) =>
  page.evaluate(() => {
    const doc = (window as any).__editor.getDocument();
    const text = (b: any): string =>
      (b.children ?? []).map((c: any) => (c.type === "text" ? c.text : c.type === "hard_break" ? "\n" : "[img]")).join("");
    return doc.blocks.map((b: any) => text(b));
  });

const noDrift = (page: Page) => page.evaluate(() => (window as any).__editor.findDrift());

/** Put the caret at the end of the last block. */
async function caretAtEnd(page: Page) {
  await page.getByTestId("editor").focus();
  await page.keyboard.press("Control+End");
}

test.describe("editor core", () => {
  test("typing, Enter, Backspace, Delete, and the model stays equal to the DOM", async ({ page }) => {
    const warnings = await open(page);
    await caretAtEnd(page);
    await page.keyboard.type("hello world");
    await page.keyboard.press("Enter");
    await page.keyboard.type("second");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "hello world", "second"]);
    await page.keyboard.press("Home");
    await page.keyboard.press("Backspace"); // join "second" onto "hello world"
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "hello worldsecond"]);
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("Delete");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "hello worlsecond"]);
    expect(await noDrift(page)).toEqual([]);
    expect(warnings).toEqual([]);
  });

  test("the first part keeps the block id on Enter; the new block gets a fresh one", async ({ page }) => {
    await open(page);
    await page.getByTestId("editor").focus();
    await page.keyboard.press("Control+Home");
    await page.keyboard.press("End");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("Enter");
    const ids = await page.evaluate(() => (window as any).__editor.getDocument().blocks.map((b: any) => b.id));
    expect(ids[0]).toBe(START);
    expect(ids).toHaveLength(4);
    expect(new Set(ids).size).toBe(4);
    expect(await blocks(page)).toEqual(["Playgrou", "nd", "Type here.", ""]);
  });

  test("emoji, ZWJ sequences and combining marks are deleted as whole characters", async ({ page }) => {
    await open(page);
    await caretAtEnd(page);
    await page.keyboard.insertText("x\u{1F468}‍\u{1F469}‍\u{1F467}\u{1F44D}\u{1F3FD}é");
    await page.keyboard.press("Backspace"); // e + combining acute
    await page.keyboard.press("Backspace"); // thumbs up + skin tone
    await page.keyboard.press("Backspace"); // the whole family
    expect((await blocks(page)).at(-1)).toBe("x");
    expect(await noDrift(page)).toEqual([]);
  });

  test("Ctrl+Backspace deletes a word; Shift+Arrow selects and typing replaces", async ({ page }) => {
    await open(page);
    await caretAtEnd(page);
    await page.keyboard.type("one two three");
    await page.keyboard.press("Control+Backspace");
    expect((await blocks(page)).at(-1)).toBe("one two ");
    for (let i = 0; i < 3; i++) await page.keyboard.press("Shift+ArrowLeft");
    await page.keyboard.type("2");
    expect((await blocks(page)).at(-1)).toBe("one t2");
  });

  test("undo and redo: typing is grouped; Enter is its own step", async ({ page }) => {
    await open(page);
    await caretAtEnd(page);
    await page.keyboard.type("abc");
    await page.keyboard.press("Enter");
    await page.keyboard.type("def");
    await page.keyboard.press("Control+z");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "abc", ""]);
    await page.keyboard.press("Control+z");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "abc"]);
    await page.keyboard.press("Control+z");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", ""]);
    await page.keyboard.press("Control+Shift+z");
    await page.keyboard.press("Control+y");
    await page.keyboard.press("Control+y");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "abc", "def"]);
    expect(await noDrift(page)).toEqual([]);
  });

  test("paste is plain text: lines become paragraphs, HTML is not imported", async ({ page, context, browserName }) => {
    test.skip(browserName !== "chromium", "clipboard permissions are Chromium-only in Playwright");
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await open(page);
    await caretAtEnd(page);
    await page.evaluate(async () => {
      const item = new ClipboardItem({
        "text/plain": new Blob(["first\r\nsecond\rthird"], { type: "text/plain" }),
        "text/html": new Blob(["<b>bold</b><script>1</script><img src=x>"], { type: "text/html" }),
      });
      await navigator.clipboard.write([item]);
    });
    await page.keyboard.press("Control+v");
    expect(await blocks(page)).toEqual(["Playground", "Type here.", "first", "second", "third"]);
    await expect(page.getByTestId("editor").locator("b, script, img")).toHaveCount(0);
  });

  test("Tab leaves the editor: there is no keyboard trap", async ({ page }) => {
    await open(page);
    await page.getByTestId("editor").focus();
    await page.keyboard.press("Tab");
    const inside = await page.evaluate(() => document.activeElement?.closest("[data-testid=editor]") !== null);
    expect(inside).toBe(false);
  });

  test("a React re-render does not disturb the text or the caret", async ({ page }) => {
    await open(page);
    await caretAtEnd(page);
    await page.keyboard.type("abc");
    await page.evaluate(() => (document.querySelector("button:nth-of-type(3)") as HTMLButtonElement).click());
    await page.keyboard.type("def");
    expect((await blocks(page)).at(-1)).toBe("abcdef");
  });

  test("dropping text into the editor does nothing", async ({ page }) => {
    await open(page);
    const before = await blocks(page);
    await page.getByTestId("editor").dispatchEvent("drop", { dataTransfer: await page.evaluateHandle(() => new DataTransfer()) });
    expect(await blocks(page)).toEqual(before);
  });

  test("IME composition: nothing is committed until it ends, then it is one undo step", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Input.imeSetComposition is a Chromium DevTools command");
    await open(page);
    await caretAtEnd(page);
    await page.keyboard.type("ab");
    const client = await page.context().newCDPSession(page);
    await client.send("Input.imeSetComposition", { text: "に", selectionStart: 1, selectionEnd: 1 });
    expect(await page.evaluate(() => (window as any).__editor.isComposing)).toBe(true);
    expect((await blocks(page)).at(-1)).toBe("ab"); // the model has not seen the composition
    await client.send("Input.imeSetComposition", { text: "にほ", selectionStart: 2, selectionEnd: 2 });
    expect((await blocks(page)).at(-1)).toBe("ab");
    await client.send("Input.insertText", { text: "日本" }); // commit
    await expect.poll(async () => (await blocks(page)).at(-1)).toBe("ab日本");
    expect(await page.evaluate(() => (window as any).__editor.isComposing)).toBe(false);
    expect(await noDrift(page)).toEqual([]);
    await page.keyboard.press("Control+z"); // undoes the composition as ONE step, not the typed "ab"
    expect((await blocks(page)).at(-1)).toBe("ab");
  });
});
