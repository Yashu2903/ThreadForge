/** SafeMermaid: hostile diagram text and hostile SVG never reach the page; the user always sees
 *  either the drawing or the source with a note. */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MAX_SVG_CHARS, SafeMermaid, drawSafely, sanitizeSvg } from "../src/ai/SafeMermaid";

const GOOD = "flowchart TD\n  A[Start] --> B[End]";
const svgOf = (inner: string) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">${inner}</svg>`;

describe("sanitizeSvg", () => {
  it.each([
    ["script", svgOf("<script>alert(1)</script><rect/>"), "<script"],
    ["inline handler", svgOf('<rect onload="alert(1)" onclick="x()"/>'), "onload"],
    ["foreignObject html", svgOf('<foreignObject><div onmouseover="x()">hi</div><iframe src="//evil"></iframe></foreignObject>'), "foreignObject"],
    ["javascript href", svgOf('<a href="javascript:alert(1)"><text>x</text></a>'), "javascript:"],
    ["use of an external file", svgOf('<use href="https://evil.example/x.svg#a"/>'), "evil.example"],
    ["external image", svgOf('<image href="https://evil.example/pixel.png"/>'), "evil.example"],
    ["animate to a script url", svgOf('<a><animate attributeName="href" values="javascript:alert(1)"/></a>'), "javascript:"],
  ])("removes %s", (_name, dirty, needle) => {
    const clean = sanitizeSvg(dirty);
    expect(clean).toContain("<svg");
    expect(clean).not.toContain(needle);
  });

  it("keeps ordinary drawing elements and text", () => {
    const clean = sanitizeSvg(svgOf('<g><rect width="4" height="4" fill="#fff"/><text x="1" y="1">Label</text><path d="M0 0L5 5"/></g>'));
    expect(clean).toContain("<rect");
    expect(clean).toContain("Label");
    expect(clean).toContain("<path");
  });
});

describe("drawSafely", () => {
  it.each([
    ["a click directive", "flowchart TD\n A-->B\n click A href \"https://evil\""],
    ["html in a label", "flowchart TD\n A[<img src=x onerror=alert(1)>] --> B"],
    ["an init directive", '%%{init: {"securityLevel":"loose"}}%%\nflowchart TD\n A-->B'],
    ["a javascript url", "flowchart TD\n A[javascript:alert(1)] --> B"],
    ["an unsupported type", "pie title x\n \"a\": 1"],
    ["nothing", "   "],
  ])("never gives %s to Mermaid", async (_name, source) => {
    const renderer = vi.fn(async () => svgOf("<rect/>"));
    const view = await drawSafely("d1", source, renderer);
    expect(view.kind).toBe("failed");
    expect(renderer).not.toHaveBeenCalled();
  });

  it("draws a good diagram through the sanitizer", async () => {
    const view = await drawSafely("d1", GOOD, async () => svgOf('<script>alert(1)</script><rect width="1" height="1"/>'));
    expect(view.kind).toBe("ok");
    if (view.kind === "ok") {
      expect(view.svg).toContain("<rect");
      expect(view.svg).not.toContain("script");
    }
  });

  it("falls back to the source when rendering throws, hangs, or returns something huge", async () => {
    expect((await drawSafely("d", GOOD, async () => Promise.reject(new Error("Parse error")))).kind).toBe("failed");
    const hung = await drawSafely("d", GOOD, () => new Promise<string>(() => undefined), 20);
    expect(hung).toMatchObject({ kind: "failed", note: expect.stringContaining("too long") });
    const huge = await drawSafely("d", GOOD, async () => svgOf("x".repeat(MAX_SVG_CHARS)));
    expect(huge).toMatchObject({ kind: "failed", note: expect.stringContaining("too large") });
    expect((await drawSafely("d", GOOD, async () => "not an svg at all")).kind).toBe("failed");
  });
});

describe("<SafeMermaid>", () => {
  it("shows the drawing for a good diagram and keeps the source available", async () => {
    render(<SafeMermaid source={GOOD} title="Flow" renderer={async () => svgOf('<rect width="1" height="1"/>')} />);
    expect(await screen.findByRole("img", { name: "Flow" })).toBeInTheDocument();
    expect(screen.getByText("Diagram source")).toBeInTheDocument();
  });

  it("never shows an empty box: a refused diagram shows its source and a plain note", async () => {
    const hostile = 'flowchart TD\n  A --> B\n  click A href "https://evil.example"';
    render(<SafeMermaid source={hostile} title="Bad" serverNote="click, href and link are not allowed." renderer={async () => svgOf("<rect/>")} />);
    await waitFor(() => expect(screen.getByRole("note")).toBeInTheDocument());
    expect(screen.getByRole("note")).toHaveTextContent(/not drawn/i);
    expect(screen.getByRole("note")).toHaveTextContent("Checker said");
    expect(screen.getByText(/click A href/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the source with a note when Mermaid cannot parse it", async () => {
    render(<SafeMermaid source={GOOD} title="Broken" renderer={async () => Promise.reject(new Error("Parse error on line 2"))} />);
    expect(await screen.findByRole("note")).toHaveTextContent(/could not be drawn/i);
    expect(screen.getByText(/A\[Start\]/)).toBeInTheDocument();
  });
});
