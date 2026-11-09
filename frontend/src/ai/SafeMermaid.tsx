/**
 * SafeMermaid: the one place a model-written diagram is drawn (chat cards now, reused by Phase 2).
 *
 * The diagram text is UNTRUSTED. Defence in depth, each layer enough on its own to stop a class of
 * attack, none relied on alone:
 *
 *   1. STATIC RULES (the shared schema package's `validateMermaidSource`, the same cases the server
 *      uses): allowed diagram types only, at most 20,000 characters and 500 lines, no `%%{ }%%`
 *      directives (they can change Mermaid's security level), no click/href/link, no HTML tags, no
 *      URLs, no javascript:/data: schemes, no entities. Refused text is never given to Mermaid.
 *   2. MERMAID CONFIGURATION, fixed here and not overridable by the diagram: `securityLevel: "strict"`
 *      (labels are encoded, click functionality off), `htmlLabels: false` for flowcharts (no HTML in
 *      labels), a fixed theme, bounded text size and edge count, no auto-start.
 *   3. A RENDER TIMEOUT (8 s) so a pathological diagram cannot hang the tab, and a SIZE LIMIT on the
 *      SVG that comes back.
 *   4. DOMPURIFY on the resulting SVG (SVG profile; script, foreignObject, iframe, use, image and all
 *      event-handler attributes removed) before it is inserted into the page.
 *
 * Whatever goes wrong, the user always sees something: the diagram, or its SOURCE with a plain note
 * saying it could not be drawn. Never an empty box.
 */
import DOMPurify from "dompurify";
import { validateMermaidSource } from "@threadforge/editor-schema";
import { useEffect, useId, useState } from "react";

export const RENDER_TIMEOUT_MS = 8_000;
export const MAX_SVG_CHARS = 2_000_000;

export function sanitizeSvg(svg: string): string {
  return DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ["script", "foreignObject", "iframe", "object", "embed", "use", "image", "a", "animate", "set"],
    FORBID_ATTR: ["style-src"],
    KEEP_CONTENT: false,
  });
}

export type Renderer = (id: string, source: string) => Promise<string>;

let configured = false;

/** Mermaid, loaded on first use (it is large) and configured once with settings the diagram cannot change. */
export const mermaidRenderer: Renderer = async (id, source) => {
  const { default: mermaid } = await import("mermaid");
  if (!configured) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "neutral",
      flowchart: { htmlLabels: false },
      maxTextSize: 20_000,
      maxEdges: 500,
      deterministicIds: true,
      logLevel: "fatal",
    });
    configured = true;
  }
  const { svg } = await mermaid.render(id, source);
  return svg;
};

type View = { kind: "pending" } | { kind: "ok"; svg: string } | { kind: "failed"; note: string };

export async function drawSafely(id: string, source: string, render: Renderer, timeoutMs = RENDER_TIMEOUT_MS): Promise<View> {
  const rules = validateMermaidSource(source);
  if (!rules.ok) return { kind: "failed", note: `This diagram was not drawn: ${rules.message}` };
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const raw = await Promise.race([
      render(id, source),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("timeout")), timeoutMs);
      }),
    ]);
    if (typeof raw !== "string" || raw.length > MAX_SVG_CHARS) return { kind: "failed", note: "This diagram is too large to draw." };
    const clean = sanitizeSvg(raw);
    if (!clean.includes("<svg")) return { kind: "failed", note: "This diagram could not be drawn." };
    return { kind: "ok", svg: clean };
  } catch (error) {
    const timedOut = error instanceof Error && error.message === "timeout";
    return { kind: "failed", note: timedOut ? "Drawing this diagram took too long." : "This diagram could not be drawn (it may contain a syntax error)." };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function SafeMermaid(props: { source: string; title: string; serverNote?: string | null; renderer?: Renderer }) {
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const [view, setView] = useState<View>({ kind: "pending" });

  useEffect(() => {
    let alive = true;
    setView({ kind: "pending" });
    void drawSafely(id, props.source, props.renderer ?? mermaidRenderer).then((v) => alive && setView(v));
    return () => {
      alive = false;
    };
  }, [id, props.source, props.renderer]);

  return (
    <figure className="diagram">
      {props.title && <figcaption>{props.title}</figcaption>}
      {view.kind === "pending" && <p className="muted">Drawing the diagram…</p>}
      {view.kind === "ok" && (
        <div
          className="diagram-svg"
          role="img"
          aria-label={props.title || "Diagram"}
          // the string went through DOMPurify (sanitizeSvg) just above
          dangerouslySetInnerHTML={{ __html: view.svg }}
        />
      )}
      {view.kind === "failed" && (
        <p role="note" className="diagram-note">
          {view.note}
          {props.serverNote ? ` Checker said: ${props.serverNote}` : ""} The source is below.
        </p>
      )}
      <details open={view.kind === "failed"}>
        <summary>Diagram source</summary>
        <pre className="diagram-source">{props.source}</pre>
      </details>
    </figure>
  );
}
