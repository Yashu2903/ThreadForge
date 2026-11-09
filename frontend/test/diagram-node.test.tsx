/** The diagram node view in a real Tiptap editor (no network, Mermaid replaced by a stand-in that records
 *  what it was given): rendered preview, "Edit source" only for people who can edit, hostile text never
 *  handed to Mermaid, and the insert command. */
import { createExtensions } from "@threadforge/editor-schema";
import type { Editor } from "@tiptap/core";
import { EditorContent, useEditor } from "@tiptap/react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { diagramNodeView } from "../src/editor/DiagramView";
import { STARTER_DIAGRAM, insertDiagram } from "../src/editor/commands";

const rendered: string[] = [];
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, source: string) => {
      rendered.push(source);
      return { svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>drawn</text></svg>' };
    }),
  },
}));

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const docWith = (source: string) => ({
  type: "doc",
  content: [
    { type: "paragraph", attrs: { id: id(1) }, content: [{ type: "text", text: "Intro" }] },
    { type: "diagram", attrs: { id: id(2) }, content: [{ type: "text", text: source }] },
  ],
});

let current: Editor | null = null;
function Harness(props: { source: string; editable: boolean }) {
  const editor = useEditor({
    extensions: createExtensions({ diagramView: diagramNodeView }),
    content: docWith(props.source),
    editable: props.editable,
    immediatelyRender: true,
  });
  current = editor;
  return <EditorContent editor={editor} />;
}

const GOOD = "flowchart TD\n  A[Start] --> B[End]";

beforeEach(() => {
  rendered.length = 0;
});

describe("the diagram node view", () => {
  it("draws the diagram and lets an editor reveal the source", async () => {
    render(<Harness source={GOOD} editable />);
    await waitFor(() => expect(screen.getByRole("img", { name: "Diagram" })).toBeTruthy());
    expect(rendered).toContain(GOOD);
    const source = screen.getByTestId("diagram-source");
    expect(source.style.display).toBe("none");
    fireEvent.click(screen.getByTestId("diagram-edit-toggle"));
    expect(screen.getByTestId("diagram-source").style.display).toBe("block");
    expect(screen.getByTestId("diagram-source").textContent).toContain("A[Start] --> B[End]");
    fireEvent.click(screen.getByTestId("diagram-edit-toggle"));
    expect(screen.getByTestId("diagram-source").style.display).toBe("none");
  });

  it("gives a viewer the picture only: no toggle, source never shown", async () => {
    render(<Harness source={GOOD} editable={false} />);
    await waitFor(() => expect(screen.getByRole("img", { name: "Diagram" })).toBeTruthy());
    expect(screen.queryByTestId("diagram-edit-toggle")).toBeNull();
    expect(screen.getByTestId("diagram-source").style.display).toBe("none");
  });

  it("never gives a hostile source to Mermaid and says why", async () => {
    render(<Harness source={'flowchart TD\n  A --> B\n  click A href "javascript:alert(1)"'} editable />);
    await waitFor(() => expect(screen.getByRole("note").textContent).toMatch(/not drawn/));
    expect(rendered).toEqual([]);
  });

  it("insertDiagram adds a strict-valid starter diagram with an id", async () => {
    render(<Harness source={GOOD} editable />);
    await waitFor(() => expect(current).not.toBeNull());
    act(() => {
      current!.commands.focus("end");
      expect(insertDiagram(current!)).toBe(true);
    });
    const json = current!.getJSON();
    const diagrams = (json.content ?? []).filter((n) => n.type === "diagram");
    expect(diagrams).toHaveLength(2);
    expect((diagrams[1]!.content?.[0] as { text?: string } | undefined)?.text).toBe(STARTER_DIAGRAM);
    expect(typeof diagrams[1]!.attrs?.id).toBe("string");
  });
});
