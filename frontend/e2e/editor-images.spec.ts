import { type Page, expect, test } from "@playwright/test";
import { AsUser, seed, signedIn } from "./helpers";

/** Image insert in a real browser, against the real API (upload pipeline, storage and serving). */

async function openRoomPlayground(browser: Parameters<Parameters<typeof test>[2]>[0]["browser"]) {
  const alice = seed("Image Owner");
  const asAlice = await AsUser.create(alice);
  const roomId = await asAlice.createRoom("Images");
  const { context, page } = await signedIn(browser, alice);
  await page.goto(`/#/playground/${roomId}`);
  await expect(page.getByTestId("editor")).toBeVisible();
  await page.getByTestId("editor").focus();
  await page.keyboard.press("Control+End");
  return { context, page, roomId, asAlice };
}

/** Make a PNG in the page with a canvas and hand it to the file input like a user choosing a file. */
async function chooseGenerated(page: Page, type = "image/png", w = 80, h = 60, name = "pic.png") {
  await page.evaluate(
    async ({ type, w, h, name }) => {
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d")!;
      ctx.fillStyle = "#cc3333";
      ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = "#3366cc";
      ctx.fillRect(0, 0, w / 2, h / 2);
      const blob = await new Promise<Blob>((r) => canvas.toBlob((b) => r(b!), type));
      const dt = new DataTransfer();
      dt.items.add(new File([blob], name, { type }));
      const input = document.querySelector("[data-testid=image-file]") as HTMLInputElement;
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    },
    { type, w, h, name },
  );
}

const lastBlock = async (page: Page) => (await page.evaluate(() => (window as any).__editor.getDocument())).blocks.at(-1);

test.describe("images", () => {
  test("insert from a file: upload, display from our server, select as a unit, alt text, delete, undo", async ({ browser }) => {
    const { context, page, roomId } = await openRoomPlayground(browser);
    await page.keyboard.type("before ");
    await chooseGenerated(page);
    const img = page.getByTestId("editor").locator("img");
    await expect(img).toHaveCount(1);
    const block = await lastBlock(page);
    const image = block.children.find((c: any) => c.type === "image");
    expect(image).toMatchObject({ type: "image", width_px: 80, height_px: 60 });
    expect(image.alt).toBeUndefined(); // default alt text is empty
    await expect(img).toHaveAttribute("src", new RegExp(`/rooms/${roomId}/assets/${image.asset_id}$`));
    // the browser really loaded it from the API (authorized by the session cookie)
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(80);

    // typing continues after the image; the caret sits after it
    await page.keyboard.type(" after");
    expect((await lastBlock(page)).children.map((c: any) => c.type)).toEqual(["text", "image", "text"]);

    // clicking the image selects it as a unit and shows the alt-text field
    await img.click();
    await expect(img).toHaveAttribute("data-selected", "true");
    await page.getByTestId("alt-input").fill("A red and blue square");
    await page.keyboard.press("Enter");
    expect((await lastBlock(page)).children[1].alt).toBe("A red and blue square");
    await expect(img).toHaveAttribute("alt", "A red and blue square");

    // Delete removes the whole image in one keystroke; Ctrl+Z brings it back, alt text included
    await img.click();
    await page.keyboard.press("Backspace");
    await expect(img).toHaveCount(0);
    expect((await lastBlock(page)).children).toEqual([{ type: "text", text: "before  after" }]);
    await page.keyboard.press("Control+z");
    await expect(img).toHaveCount(1);
    expect((await lastBlock(page)).children[1].alt).toBe("A red and blue square");
    expect(await page.evaluate(() => (window as any).__editor.findDrift())).toEqual([]);
    await context.close();
  });

  test("Backspace right after an image deletes it as one unit", async ({ browser }) => {
    const { context, page } = await openRoomPlayground(browser);
    await chooseGenerated(page);
    await expect(page.getByTestId("editor").locator("img")).toHaveCount(1);
    await page.keyboard.press("Backspace");
    await expect(page.getByTestId("editor").locator("img")).toHaveCount(0);
    await context.close();
  });

  test("files the server refuses explain why, and nothing is inserted", async ({ browser }) => {
    const { context, page } = await openRoomPlayground(browser);
    // an SVG labelled as an image: refused by content type
    await chooseGenerated(page, "image/png", 10, 10, "ok.png"); // control: this one works
    await expect(page.getByTestId("editor").locator("img")).toHaveCount(1);
    await page.evaluate(async () => {
      const svg = new File(["<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"], "x.svg", { type: "image/svg+xml" });
      const dt = new DataTransfer();
      dt.items.add(svg);
      const input = document.querySelector("[data-testid=image-file]") as HTMLInputElement;
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await expect(page.getByTestId("notice")).toContainText("Only PNG, JPEG and WebP");
    // a PNG header followed by junk: the server decodes it for real and refuses
    await page.evaluate(async () => {
      const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, ...new Array(200).fill(7)]);
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], "bad.png", { type: "image/png" }));
      const input = document.querySelector("[data-testid=image-file]") as HTMLInputElement;
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await expect(page.getByTestId("notice")).toContainText("not a valid image");
    await expect(page.getByTestId("editor").locator("img")).toHaveCount(1);
    await context.close();
  });

  test("a viewer upload is refused by the server with a clear message", async ({ browser }) => {
    const { context, page, roomId, asAlice } = await openRoomPlayground(browser);
    const viewer = seed("Image Viewer", { roomId, role: "viewer" });
    const v = await signedIn(browser, viewer);
    await v.page.goto(`/#/playground/${roomId}`);
    await expect(v.page.getByTestId("editor")).toBeVisible();
    await v.page.getByTestId("editor").focus();
    await chooseGenerated(v.page);
    await expect(v.page.getByTestId("notice")).toContainText("does not allow");
    await expect(v.page.getByTestId("editor").locator("img")).toHaveCount(0);
    await v.context.close();
    await context.close();
    await asAlice.dispose();
  });
});
