import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Doc } from "../src/editor/doc";
import type { Editor } from "../src/editor/editor";
import { EditorSurface } from "../src/editor/EditorSurface";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const START: Doc = { schema_version: 1, blocks: [{ type: "paragraph", id: id(1), children: [{ type: "text", text: "typed" }] }] };

function Parent(props: { onReady: (e: Editor) => void; onChange?: () => void }) {
  const [count, setCount] = useState(0);
  const [readOnly, setReadOnly] = useState(false);
  return (
    <div>
      <button onClick={() => setCount(count + 1)}>rerender {count}</button>
      <button onClick={() => setReadOnly(!readOnly)}>toggle read-only</button>
      <EditorSurface initialDocument={START} readOnly={readOnly} onReady={props.onReady} onChange={props.onChange} label="Doc" />
    </div>
  );
}

describe("EditorSurface", () => {
  it("renders one empty element; the editor, not React, fills it", () => {
    let editor!: Editor;
    render(<Parent onReady={(e) => (editor = e)} />);
    const host = screen.getByTestId("editor");
    expect(host).toHaveAttribute("role", "textbox");
    expect(host).toHaveAttribute("aria-label", "Doc");
    expect(host.querySelector("p")?.textContent).toBe("typed");
    expect(editor.getDocument()).toBe(START);
  });

  it("React re-rendering the parent never touches the editable root", async () => {
    render(<Parent onReady={() => undefined} />);
    const host = screen.getByTestId("editor");
    const records: MutationRecord[] = [];
    const observer = new MutationObserver((r) => records.push(...r));
    observer.observe(host, { subtree: true, childList: true, characterData: true, attributes: true });
    for (let i = 0; i < 5; i++) await userEvent.click(screen.getByRole("button", { name: /rerender/ }));
    await Promise.resolve();
    observer.disconnect();
    expect(records).toHaveLength(0);
  });

  it("read-only is switched through the editor's method, not by re-rendering its content", async () => {
    render(<Parent onReady={() => undefined} />);
    const host = screen.getByTestId("editor");
    const p = host.querySelector("p");
    await userEvent.click(screen.getByRole("button", { name: "toggle read-only" }));
    expect(host).toHaveAttribute("contenteditable", "false");
    expect(host.querySelector("p")).toBe(p);
  });

  it("changes made through the editor reach the callback; the latest callback is used", () => {
    const first = vi.fn();
    let editor!: Editor;
    const { rerender } = render(<Parent onReady={(e) => (editor = e)} onChange={first} />);
    const second = vi.fn();
    rerender(<Parent onReady={(e) => (editor = e)} onChange={second} />);
    const host = screen.getByTestId("editor");
    const p = host.querySelector("p")!;
    act(() => {
      editor.setSelection({ anchor: { id: id(1), offset: 5 }, focus: { id: id(1), offset: 5 } });
      host.dispatchEvent(new InputEvent("beforeinput", { inputType: "insertText", data: "!", cancelable: true, bubbles: true }));
    });
    expect(p.textContent).toBe("typed!");
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("survives React StrictMode's mount, unmount, mount", () => {
    render(
      <StrictMode>
        <Parent onReady={() => undefined} />
      </StrictMode>,
    );
    const host = screen.getByTestId("editor");
    expect(host.querySelectorAll("p")).toHaveLength(1);
    expect(host).toHaveAttribute("contenteditable", "true");
  });
});
