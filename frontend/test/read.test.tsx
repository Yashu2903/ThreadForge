import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentRead } from "../src/components/DocumentRead";
import { Api, ApiError } from "../src/api";
import { csrfRoute, fakeApi } from "./helpers";

const p = (id: string, ...children: object[]) => ({ type: "paragraph" as const, id, children: children as never });
const doc = (...blocks: object[]) => ({ schema_version: 1 as const, blocks: blocks as never });

describe("DocumentRead", () => {
  it("renders text as text: markup in the content is never interpreted", () => {
    const { container } = render(
      <DocumentRead roomId="r" content={doc(p("a", { type: "text", text: '<img src=x onerror="alert(1)"><script>alert(2)</script>' }))} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(2)</script>");
  });

  it("renders marks, size, colour and a safe link", () => {
    const { container } = render(
      <DocumentRead
        roomId="r"
        content={doc(
          p(
            "a",
            { type: "text", text: "bold", marks: { bold: true, italic: true, size_pt: 14, color: "#FF0000", highlight: "#FFFF00", font: "serif" } },
            { type: "text", text: "go", marks: { link: { href: "https://example.com" } } },
          ),
        )}
      />,
    );
    expect(container.querySelector("strong em, em strong")).not.toBeNull();
    const span = container.querySelector("span.font-serif") as HTMLElement;
    expect(span.style.fontSize).toBe("14pt");
    expect(span.style.color).toBe("rgb(255, 0, 0)");
    const link = screen.getByRole("link", { name: "go" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("renders paragraph attributes without any CSS string from the document", () => {
    const { container } = render(
      <DocumentRead
        roomId="r"
        content={doc({ type: "paragraph", id: "a", attrs: { align: "center", line_spacing: 1.5, space_after_pt: 12, indent_level: 2 }, children: [{ type: "text", text: "x" }] })}
      />,
    );
    const para = container.querySelector("p[data-id=a]") as HTMLElement;
    expect(para.style.textAlign).toBe("center");
    expect(para.style.lineHeight).toBe("1.5");
    expect(para.style.marginBottom).toBe("12pt");
    expect(para.style.marginLeft).toBe("72pt");
  });

  it("renders headings, quotes, nested lists and tables with header cells", () => {
    const { container } = render(
      <DocumentRead
        roomId="r"
        content={doc(
          { type: "heading", id: "h", level: 2, children: [{ type: "text", text: "Title" }] },
          { type: "quote", id: "q", children: [p("q1", { type: "text", text: "quoted" })] },
          { type: "list", id: "l", ordered: true, children: [{ type: "list_item", id: "i1", children: [p("lp", { type: "text", text: "one" }), { type: "list", id: "l2", children: [{ type: "list_item", id: "i2", children: [p("lp2", { type: "text", text: "nested" })] }] }] }] },
          { type: "table", id: "t", children: [{ type: "table_row", id: "r1", children: [{ type: "table_cell", id: "c1", header: true, children: [p("tp", { type: "text", text: "H" })] }, { type: "table_cell", id: "c2", children: [p("tp2")] }] }] },
        )}
      />,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Title" })).toBeInTheDocument();
    expect(container.querySelector("blockquote")?.textContent).toBe("quoted");
    expect(container.querySelector("ol > li > ul li")?.textContent).toBe("nested");
    expect(container.querySelector("th")?.textContent).toBe("H");
    expect(container.querySelectorAll("td")).toHaveLength(1);
  });

  it("renders images from the room asset URL with their alt text and size", () => {
    const { container } = render(
      <DocumentRead roomId="room1" content={doc(p("a", { type: "image", id: "i", asset_id: "asset1", width_px: 40, height_px: 30, alt: "A chart" }))} />,
    );
    const img = container.querySelector("img")!;
    expect(img.getAttribute("src")).toBe("/rooms/room1/assets/asset1");
    expect(img).toHaveAttribute("alt", "A chart");
    expect(img).toHaveAttribute("width", "40");
  });

  it("keeps an empty paragraph visible and the paper class (always light)", () => {
    const { container } = render(<DocumentRead roomId="r" content={doc(p("a"))} />);
    expect(container.querySelector("p br")).not.toBeNull();
    expect(container.querySelector(".paper")).not.toBeNull();
  });
});

describe("Api", () => {
  it("sends the CSRF token on unsafe methods only", async () => {
    const { api, calls } = fakeApi({ ...csrfRoute, "GET /x": () => ({ body: {} }), "PUT /x": () => ({ body: {} }) });
    await api.get("/x");
    await api.send("PUT", "/x", { a: 1 });
    expect(calls.find((c) => c.method === "GET" && c.path === "/x")!.headers["X-CSRF-Token"]).toBeUndefined();
    expect(calls.find((c) => c.method === "PUT")!.headers["X-CSRF-Token"]).toBe("tok");
  });

  it("fetches a fresh token once when the server says csrf_failed, then repeats the request", async () => {
    let token = 0;
    let puts = 0;
    const { api, calls } = fakeApi({
      "GET /auth/csrf": () => ({ body: { csrf_token: `tok${++token}` } }),
      "PUT /x": (call) => {
        puts++;
        return call.headers["X-CSRF-Token"] === "tok2" ? { body: { ok: true } } : { status: 403, body: { error: { code: "csrf_failed", message: "bad" } } };
      },
    });
    const res = await api.send("PUT", "/x", {});
    expect(res.body).toEqual({ ok: true });
    expect(puts).toBe(2);
    expect(calls.filter((c) => c.path === "/auth/csrf")).toHaveLength(2);
  });

  it("turns the error envelope into an ApiError with its extra fields and Retry-After", async () => {
    const { api } = fakeApi({
      ...csrfRoute,
      "PUT /x": () => ({ status: 409, body: { error: { code: "document_version_conflict", message: "m", request_id: "r", current_content_version: 7 } }, headers: { "Retry-After": "3" } }),
    });
    const err = (await api.send("PUT", "/x", {}).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.extra, err.retryAfter]).toEqual([409, "document_version_conflict", { current_content_version: 7 }, 3]);
  });

  it("survives a non-JSON error page", async () => {
    const api = new Api("", async () => new Response("<html>bad gateway</html>", { status: 502 }));
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect([err.status, err.code]).toEqual([502, "http_error"]);
  });
});
