import { describe, expect, it } from "vitest";
import { sanitizePastedHtml } from "../src/editor/TiptapEditor";

const KNOWN = "11111111-1111-4111-8111-111111111111";

describe("pasted HTML", () => {
  it("removes every picture that is not a copy of one already in the document", () => {
    const html = [
      "<p>text</p>",
      '<img src="https://evil.example/x.png">',
      '<img src="data:image/png;base64,AAAA">',
      '<img data-asset-id="22222222-2222-4222-8222-222222222222" src="/x">',
      `<img data-asset-id="${KNOWN}" src="/rooms/r/assets/${KNOWN}">`,
    ].join("");
    const out = sanitizePastedHtml(html, new Set([KNOWN]));
    const doc = new DOMParser().parseFromString(out, "text/html");
    const imgs = Array.from(doc.querySelectorAll("img"));
    expect(imgs).toHaveLength(1);
    expect(imgs[0]!.getAttribute("data-asset-id")).toBe(KNOWN);
    expect(doc.body.textContent).toBe("text");
  });

  it("keeps ordinary markup and drops all pictures when none are known", () => {
    const out = sanitizePastedHtml(`<h1>Title</h1><p><b>bold</b> <img data-asset-id="${KNOWN}"></p>`, new Set());
    expect(out).toContain("<b>bold</b>");
    expect(out).not.toContain("<img");
  });
});
