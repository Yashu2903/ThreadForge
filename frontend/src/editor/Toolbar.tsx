import { type ReactNode, useEffect, useRef, useState } from "react";
import type { Editor } from "./editor";
import { FONT_OPTIONS, SIZE_OPTIONS } from "./fonts";
import type { BlockKind } from "./format";

/**
 * The formatting toolbar. Accessibility rules it follows:
 *  - role="toolbar" with ONE tab stop (roving tabindex): Tab enters, Left/Right/Home/End move between
 *    controls, Tab again leaves (no keyboard trap);
 *  - every control has an accessible name and a tooltip; toggles use aria-pressed; unavailable
 *    controls use aria-disabled (they stay focusable and say why) instead of disappearing;
 *  - buttons do not take focus from the text on mouse down (preventDefault on mousedown), so the
 *    editor's selection survives a click; controls that must take focus (selects, colour, the link
 *    and table forms) work on the editor's remembered selection and hand focus back afterwards.
 * The toolbar is React; it only calls the editor's methods and reads editor.formatState().
 */

interface Props {
  editor: Editor | null;
  /** bump this when the editor's state changed, so the toolbar re-reads it */
  revision: number;
  linkOpen: boolean;
  onLinkOpenChange: (open: boolean) => void;
  /** Upload a chosen file and return the asset (the host knows the room). Omit to hide Insert image. */
  uploadImage?: (file: File) => Promise<{ id: string; width_px: number; height_px: number }>;
  onNotice?: (message: string) => void;
}

const BLOCKS: { value: BlockKind; label: string }[] = [
  { value: "paragraph", label: "Paragraph" },
  { value: "h1", label: "Heading 1" },
  { value: "h2", label: "Heading 2" },
  { value: "h3", label: "Heading 3" },
];
const SPACING = [0, 3, 6, 8, 10, 12, 18, 24, 36, 48, 72];

export function Toolbar({ editor, revision, linkOpen, onLinkOpenChange, uploadImage, onNotice }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  const [tableOpen, setTableOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  void revision;
  const s = editor ? editor.formatState() : null;
  const off = !editor || !s || s.readOnly;

  // roving tabindex: exactly one control is a tab stop
  useEffect(() => {
    const items = items_(ref.current);
    items.forEach((el, i) => el.setAttribute("tabindex", i === Math.min(active, items.length - 1) ? "0" : "-1"));
  });

  function onKeyDown(e: React.KeyboardEvent) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const target = e.target as HTMLElement;
    if (target.tagName === "INPUT" && (target as HTMLInputElement).type === "text") return; // caret movement in a text field
    const items = items_(ref.current);
    const at = items.indexOf(target);
    if (at < 0) return;
    e.preventDefault();
    const next = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (at + (e.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
    setActive(next);
    items[next]!.focus();
  }

  const btn = (name: string, run: () => void, opts: { pressed?: boolean; disabled?: boolean; shortcut?: string; children: ReactNode; testid?: string }) => (
    <button
      type="button"
      data-toolbar-item
      data-testid={opts.testid}
      aria-label={name}
      title={opts.shortcut ? `${name} (${opts.shortcut})` : name}
      aria-pressed={opts.pressed === undefined ? undefined : opts.pressed}
      aria-disabled={opts.disabled || off ? true : undefined}
      onMouseDown={(e) => e.preventDefault()} // keep the text selection
      onClick={() => {
        if (opts.disabled || off) return;
        run();
      }}
    >
      {opts.children}
    </button>
  );

  const mod = typeof navigator !== "undefined" && /Mac/i.test(navigator.platform) ? "Cmd" : "Ctrl";
  const pressed = (v: unknown) => v === "on";
  const select = (name: string, value: string, options: { value: string; label: string }[], onChange: (v: string) => void, testid?: string) => (
    <select
      data-toolbar-item
      data-testid={testid}
      aria-label={name}
      title={name}
      value={value}
      aria-disabled={off ? true : undefined}
      disabled={off}
      onChange={(e) => {
        onChange(e.target.value);
        editor?.focus();
      }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );

  if (!s) return <div role="toolbar" aria-label="Formatting" className="toolbar" ref={ref} />;
  const sizeValue = s.size === "mixed" ? "mixed" : s.size === null ? "" : String(s.size);
  const fontValue = s.font === "mixed" ? "mixed" : s.font === null ? "" : String(s.font);
  const blockValue = s.block === "mixed" ? "mixed" : s.block;
  const withMixed = <T extends { value: string; label: string }>(items: T[], current: string) =>
    current === "mixed" ? [{ value: "mixed", label: "(mixed)" }, ...items] : items;

  return (
    <div role="toolbar" aria-label="Formatting" aria-orientation="horizontal" className="toolbar" ref={ref} onKeyDown={onKeyDown}>
      <div className="tb-group" role="group" aria-label="History">
        {btn("Undo", () => editor!.undo(), { shortcut: `${mod}+Z`, disabled: !s.canUndo, children: "↶", testid: "tb-undo" })}
        {btn("Redo", () => editor!.redo(), { shortcut: `${mod}+Shift+Z`, disabled: !s.canRedo, children: "↷", testid: "tb-redo" })}
      </div>

      <div className="tb-group" role="group" aria-label="Text style">
        {select("Text style", blockValue, withMixed(BLOCKS, blockValue), (v) => editor!.setBlockKind(v as BlockKind), "tb-block")}
        {btn("Quote", () => editor!.toggleQuote(), { pressed: s.quote, children: "❝", testid: "tb-quote" })}
      </div>

      <div className="tb-group" role="group" aria-label="Font">
        {select(
          "Font",
          fontValue,
          withMixed([{ value: "", label: "Default font" }, ...FONT_OPTIONS.map((f) => ({ value: f.key, label: f.label }))], fontValue),
          (v) => editor!.setMarkValue("font", v === "" || v === "mixed" ? null : v),
          "tb-font",
        )}
        {select(
          "Font size",
          sizeValue,
          withMixed([{ value: "", label: "Size" }, ...SIZE_OPTIONS.map((n) => ({ value: String(n), label: String(n) }))], sizeValue),
          (v) => editor!.setMarkValue("size_pt", v === "" || v === "mixed" ? null : Number(v)),
          "tb-size",
        )}
      </div>

      <div className="tb-group" role="group" aria-label="Character formatting">
        {btn("Bold", () => editor!.toggleMark("bold"), { pressed: pressed(s.bold), shortcut: `${mod}+B`, children: <b>B</b>, testid: "tb-bold" })}
        {btn("Italic", () => editor!.toggleMark("italic"), { pressed: pressed(s.italic), shortcut: `${mod}+I`, children: <i>I</i>, testid: "tb-italic" })}
        {btn("Underline", () => editor!.toggleMark("underline"), { pressed: pressed(s.underline), shortcut: `${mod}+U`, children: <u>U</u>, testid: "tb-underline" })}
        {btn("Strikethrough", () => editor!.toggleMark("strike"), { pressed: pressed(s.strike), children: <s>S</s>, testid: "tb-strike" })}
        {btn("Superscript", () => editor!.setMarkValue("script", s.script === "superscript" ? null : "superscript"), { pressed: s.script === "superscript", children: "x²", testid: "tb-sup" })}
        {btn("Subscript", () => editor!.setMarkValue("script", s.script === "subscript" ? null : "subscript"), { pressed: s.script === "subscript", children: "x₂", testid: "tb-sub" })}
        <label className="color-pick" title="Text colour">
          <span className="sr-only">Text colour</span>
          <input
            type="color"
            data-toolbar-item
            data-testid="tb-color"
            aria-label="Text colour"
            aria-disabled={off ? true : undefined}
            disabled={off}
            value={typeof s.color === "string" ? s.color.toLowerCase() : "#000000"}
            onChange={(e) => editor!.setMarkValue("color", e.target.value)}
          />
        </label>
        <label className="color-pick" title="Highlight colour">
          <span className="sr-only">Highlight colour</span>
          <input
            type="color"
            data-toolbar-item
            data-testid="tb-highlight"
            aria-label="Highlight colour"
            aria-disabled={off ? true : undefined}
            disabled={off}
            value={typeof s.highlight === "string" ? s.highlight.toLowerCase() : "#ffffff"}
            onChange={(e) => editor!.setMarkValue("highlight", e.target.value)}
          />
        </label>
        {btn("Clear colours", () => {
          editor!.setMarkValue("color", null);
          editor!.setMarkValue("highlight", null);
        }, { children: "⌫", testid: "tb-clear-colors" })}
      </div>

      <div className="tb-group" role="group" aria-label="Paragraph">
        {(["left", "center", "right", "justify"] as const).map((a) =>
          btn(`Align ${a}`, () => editor!.setAttrs({ align: a }), { pressed: s.align === a, children: { left: "⇤", center: "↔", right: "⇥", justify: "☰" }[a], testid: `tb-align-${a}` }),
        )}
        {select(
          "Line spacing",
          String(s.lineSpacing),
          withMixed([1, 1.15, 1.5, 2].map((n) => ({ value: String(n), label: `${n}×` })), String(s.lineSpacing)),
          (v) => editor!.setAttrs({ line_spacing: Number(v) }),
          "tb-line-spacing",
        )}
        {select(
          "Space before paragraph",
          String(s.spaceBefore),
          withMixed(SPACING.map((n) => ({ value: String(n), label: `↑ ${n} pt` })), String(s.spaceBefore)),
          (v) => editor!.setAttrs({ space_before_pt: Number(v) }),
          "tb-space-before",
        )}
        {select(
          "Space after paragraph",
          String(s.spaceAfter),
          withMixed(SPACING.map((n) => ({ value: String(n), label: `↓ ${n} pt` })), String(s.spaceAfter)),
          (v) => editor!.setAttrs({ space_after_pt: Number(v) }),
          "tb-space-after",
        )}
        {btn("Decrease indent", () => editor!.outdent(), { shortcut: "Shift+Tab in a list", children: "⇠", testid: "tb-outdent" })}
        {btn("Increase indent", () => editor!.indent(), { shortcut: "Tab in a list", children: "⇢", testid: "tb-indent" })}
      </div>

      <div className="tb-group" role="group" aria-label="Lists and links">
        {btn("Bulleted list", () => editor!.toggleList(false), { pressed: !!s.list && s.list.ordered === false, children: "•≡", testid: "tb-bullets" })}
        {btn("Numbered list", () => editor!.toggleList(true), { pressed: !!s.list && s.list.ordered === true, children: "1.", testid: "tb-numbers" })}
        {btn("Link", () => onLinkOpenChange(!linkOpen), { pressed: linkOpen || !!s.link, shortcut: `${mod}+K`, children: "🔗", testid: "tb-link" })}
      </div>

      {(uploadImage || s.image) && (
        <div className="tb-group image-group" role="group" aria-label="Image">
          {uploadImage &&
            btn("Insert image", () => fileInput.current?.click(), {
              disabled: uploading,
              children: uploading ? "…" : "🖼",
              testid: "tb-image",
            })}
          {uploadImage && (
            <input
              ref={fileInput}
              type="file"
              className="sr-only"
              tabIndex={-1}
              accept="image/png,image/jpeg,image/webp"
              data-testid="image-file"
              aria-label="Choose an image to insert"
              onChange={async (e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (!file || !editor) return;
                setUploading(true);
                try {
                  const asset = await uploadImage(file);
                  editor.insertImage({ asset_id: asset.id, width_px: asset.width_px, height_px: asset.height_px });
                } catch (err) {
                  onNotice?.(err instanceof Error ? err.message : "The image could not be uploaded.");
                } finally {
                  setUploading(false);
                }
              }}
            />
          )}
          {s.image && <AltText key={s.image.id} editor={editor!} id={s.image.id} initial={s.image.alt} />}
        </div>
      )}

      <div className="tb-group" role="group" aria-label="Table">
        {btn("Insert table", () => setTableOpen(!tableOpen), { pressed: tableOpen, children: "▦", testid: "tb-table" })}
        {s.table && (
          <>
            {btn("Add row above", () => editor!.addRow("above"), { children: "⤒", testid: "tb-row-above" })}
            {btn("Add row below", () => editor!.addRow("below"), { children: "⤓", testid: "tb-row-below" })}
            {btn("Add column left", () => editor!.addColumn("left"), { children: "⇤▮", testid: "tb-col-left" })}
            {btn("Add column right", () => editor!.addColumn("right"), { children: "▮⇥", testid: "tb-col-right" })}
            {btn("Delete row", () => editor!.removeRow(), { disabled: s.table.rows === 1, children: "−row", testid: "tb-del-row" })}
            {btn("Delete column", () => editor!.removeColumn(), { disabled: s.table.cols === 1, children: "−col", testid: "tb-del-col" })}
            {btn("Delete table", () => editor!.deleteTable(), { children: "🗑", testid: "tb-del-table" })}
          </>
        )}
      </div>

      {linkOpen && editor && <LinkForm editor={editor} initial={s.link?.href ?? ""} hasLink={!!s.link} onClose={() => onLinkOpenChange(false)} />}
      {tableOpen && editor && <TableForm editor={editor} onClose={() => setTableOpen(false)} />}
      {s.multiCell && (
        <p className="tb-hint" role="status">
          This selection spans table cells: formatting works, typing and deleting do not.
        </p>
      )}
    </div>
  );
}

function AltText(props: { editor: Editor; id: string; initial: string }) {
  const [value, setValue] = useState(props.initial);
  const commit = () => value !== props.initial && props.editor.setImageAlt(props.id, value);
  return (
    <label className="alt-field">
      <span className="sr-only">Image description (alt text)</span>
      <input
        type="text"
        data-testid="alt-input"
        aria-label="Image description (alt text)"
        placeholder="Describe the image"
        maxLength={500}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
            props.editor.focus();
          }
        }}
      />
    </label>
  );
}

function items_(root: HTMLElement | null): HTMLElement[] {
  return root ? Array.from(root.querySelectorAll<HTMLElement>("[data-toolbar-item]:not(:disabled)")) : [];
}

function LinkForm(props: { editor: Editor; initial: string; hasLink: boolean; onClose: () => void }) {
  const [href, setHref] = useState(props.initial);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  function apply(e: React.FormEvent) {
    e.preventDefault();
    if (props.editor.setLink(href.trim())) props.onClose();
  }
  return (
    <form className="tb-form" onSubmit={apply} role="group" aria-label="Link" onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), props.onClose(), props.editor.focus())}>
      <label>
        Link address
        <input ref={input} type="text" value={href} onChange={(e) => setHref(e.target.value)} placeholder="https://example.com" data-testid="link-input" />
      </label>
      <button type="submit" className="primary" data-testid="link-apply">
        Apply
      </button>
      {props.hasLink && (
        <button type="button" data-testid="link-remove" onClick={() => (props.editor.removeLink(), props.onClose())}>
          Remove link
        </button>
      )}
      <button type="button" onClick={() => (props.onClose(), props.editor.focus())}>
        Cancel
      </button>
    </form>
  );
}

function TableForm(props: { editor: Editor; onClose: () => void }) {
  const [rows, setRows] = useState(3);
  const [cols, setCols] = useState(3);
  const [header, setHeader] = useState(true);
  return (
    <form
      className="tb-form"
      role="group"
      aria-label="Insert table"
      onSubmit={(e) => {
        e.preventDefault();
        if (props.editor.insertTable(rows, cols, header)) props.onClose();
      }}
      onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), props.onClose(), props.editor.focus())}
    >
      <label>
        Rows
        <input type="number" min={1} max={50} value={rows} onChange={(e) => setRows(Number(e.target.value))} data-testid="table-rows" />
      </label>
      <label>
        Columns
        <input type="number" min={1} max={20} value={cols} onChange={(e) => setCols(Number(e.target.value))} data-testid="table-cols" />
      </label>
      <label className="check">
        <input type="checkbox" checked={header} onChange={(e) => setHeader(e.target.checked)} /> Header row
      </label>
      <button type="submit" className="primary" data-testid="table-insert">
        Insert
      </button>
      <button type="button" onClick={() => (props.onClose(), props.editor.focus())}>
        Cancel
      </button>
    </form>
  );
}
