import { LINE_SPACINGS } from "@threadforge/editor-schema";
import type { Editor } from "@tiptap/core";
import { Fragment, type ReactNode, useEffect, useRef, useState } from "react";
import {
  type BlockKind,
  applyLink,
  changeIndent,
  clearColors,
  insertAssetImage,
  insertDiagram,
  insertTable,
  removeLink,
  setAlign,
  setBlock,
  setBlockAttrs,
  setHighlight,
  setImageAlt,
  setScript,
  setTextStyle,
  tableCommand,
} from "./commands";
import { FONT_OPTIONS, SIZE_OPTIONS } from "./fonts";
import { type FormatState, type Tri, readFormatState } from "./formatState";

/**
 * The formatting toolbar, built on Tiptap commands (see commands.ts). Accessibility rules:
 *  - role="toolbar" with ONE tab stop (roving tabindex): Tab enters, Left/Right/Home/End move between
 *    controls, Tab again leaves (no keyboard trap);
 *  - every control has an accessible name and a tooltip with its shortcut; toggles use aria-pressed
 *    ("mixed" when the selection is partly formatted); unavailable controls use aria-disabled (they
 *    stay focusable) instead of disappearing;
 *  - buttons do not take focus from the text on mouse down, so the editor's selection survives a click.
 * Link and image-description editing use small forms (popovers) that return focus to the text.
 * Every control calls a command that changes the shared document: there is no control without effect.
 */
interface Props {
  editor: Editor | null;
  /** bump to make the toolbar re-read the editor (set from the editor's transactions) */
  revision: number;
  /** false: the person cannot edit (read-only, offline-forbidden, access lost) */
  enabled: boolean;
  linkOpen: boolean;
  onLinkOpenChange: (open: boolean) => void;
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
const MIXED = "mixed";

const triPressed = (v: Tri): boolean | "mixed" => (v === "on" ? true : v === "mixed" ? "mixed" : false);

export function Toolbar({ editor, revision, enabled, linkOpen, onLinkOpenChange, uploadImage, onNotice }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  const [tableOpen, setTableOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  void revision;
  const s: FormatState | null = editor && !editor.isDestroyed ? readFormatState(editor) : null;
  const off = !editor || !s || !enabled;

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

  const mod = typeof navigator !== "undefined" && /Mac/i.test(navigator.platform) ? "Cmd" : "Ctrl";

  const btn = (name: string, run: () => void, opts: { pressed?: boolean | "mixed"; disabled?: boolean; shortcut?: string; children: ReactNode; testid?: string }) => (
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
        editor?.commands.focus();
      }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );

  if (!s || !editor) return <div role="toolbar" aria-label="Formatting" className="toolbar" ref={ref} />;
  const str = (v: unknown) => (v === MIXED ? MIXED : v === null || v === undefined ? "" : String(v));
  const withMixed = <T extends { value: string; label: string }>(items: T[], current: string) =>
    current === MIXED ? [{ value: MIXED, label: "(mixed)" }, ...items] : items;
  const numOrNull = (v: string): number | null => (v === "" || v === MIXED ? null : Number(v));

  return (
    <div role="toolbar" aria-label="Formatting" aria-orientation="horizontal" className="toolbar" ref={ref} onKeyDown={onKeyDown}>
      <div className="tb-group" role="group" aria-label="History">
        {btn("Undo", () => editor.chain().focus().undo().run(), { shortcut: `${mod}+Z`, disabled: !s.canUndo, children: "↶", testid: "tb-undo" })}
        {btn("Redo", () => editor.chain().focus().redo().run(), { shortcut: `${mod}+Shift+Z`, disabled: !s.canRedo, children: "↷", testid: "tb-redo" })}
      </div>

      <div className="tb-group" role="group" aria-label="Text style">
        {select("Text style", str(s.block), withMixed(BLOCKS, str(s.block)), (v) => v !== MIXED && setBlock(editor, v as BlockKind), "tb-block")}
        {btn("Quote", () => editor.chain().focus().toggleBlockquote().run(), { pressed: s.quote, children: "❝", testid: "tb-quote" })}
      </div>

      <div className="tb-group" role="group" aria-label="Font">
        {select(
          "Font",
          str(s.font),
          withMixed([{ value: "", label: "Default font" }, ...FONT_OPTIONS.map((f) => ({ value: f.key, label: f.label }))], str(s.font)),
          (v) => v !== MIXED && setTextStyle(editor, "fontKey", v === "" ? null : v),
          "tb-font",
        )}
        {select(
          "Font size",
          str(s.size),
          withMixed([{ value: "", label: "Size" }, ...SIZE_OPTIONS.map((n) => ({ value: String(n), label: String(n) }))], str(s.size)),
          (v) => v !== MIXED && setTextStyle(editor, "fontSizePt", v === "" ? null : Number(v)),
          "tb-size",
        )}
      </div>

      <div className="tb-group" role="group" aria-label="Character formatting">
        {btn("Bold", () => editor.chain().focus().toggleBold().run(), { pressed: triPressed(s.bold), shortcut: `${mod}+B`, children: <b>B</b>, testid: "tb-bold" })}
        {btn("Italic", () => editor.chain().focus().toggleItalic().run(), { pressed: triPressed(s.italic), shortcut: `${mod}+I`, children: <i>I</i>, testid: "tb-italic" })}
        {btn("Underline", () => editor.chain().focus().toggleUnderline().run(), { pressed: triPressed(s.underline), shortcut: `${mod}+U`, children: <u>U</u>, testid: "tb-underline" })}
        {btn("Strikethrough", () => editor.chain().focus().toggleStrike().run(), { pressed: triPressed(s.strike), shortcut: `${mod}+Shift+S`, children: <s>S</s>, testid: "tb-strike" })}
        {btn("Superscript", () => setScript(editor, s.script === "superscript" ? null : "superscript"), { pressed: s.script === MIXED ? "mixed" : s.script === "superscript", children: "x²", testid: "tb-sup" })}
        {btn("Subscript", () => setScript(editor, s.script === "subscript" ? null : "subscript"), { pressed: s.script === MIXED ? "mixed" : s.script === "subscript", children: "x₂", testid: "tb-sub" })}
        <label className="color-pick" title="Text colour">
          <span className="sr-only">Text colour</span>
          <input
            type="color"
            data-toolbar-item
            data-testid="tb-color"
            aria-label="Text colour"
            aria-disabled={off ? true : undefined}
            disabled={off}
            value={typeof s.color === "string" && s.color !== MIXED ? s.color.toLowerCase() : "#000000"}
            onChange={(e) => setTextStyle(editor, "color", e.target.value.toUpperCase())}
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
            value={typeof s.highlight === "string" && s.highlight !== MIXED ? s.highlight.toLowerCase() : "#ffffff"}
            onChange={(e) => setHighlight(editor, e.target.value.toUpperCase())}
          />
        </label>
        {btn("Clear colours", () => clearColors(editor), { children: "⌫", testid: "tb-clear-colors" })}
      </div>

      <div className="tb-group" role="group" aria-label="Paragraph">
        {(["left", "center", "right", "justify"] as const).map((a) => (
          <Fragment key={a}>
            {btn(`Align ${a}`, () => setAlign(editor, a), {
              pressed: s.align === MIXED ? "mixed" : s.align === a,
              children: { left: "⇤", center: "↔", right: "⇥", justify: "☰" }[a],
              testid: `tb-align-${a}`,
            })}
          </Fragment>
        ))}
        {select(
          "Line spacing",
          str(s.lineSpacing),
          withMixed([{ value: "", label: "Spacing" }, ...LINE_SPACINGS.map((n) => ({ value: String(n), label: `${n}×` }))], str(s.lineSpacing)),
          (v) => v !== MIXED && setBlockAttrs(editor, { lineSpacing: numOrNull(v) }),
          "tb-line-spacing",
        )}
        {select(
          "Space before paragraph",
          str(s.spaceBefore),
          withMixed([{ value: "", label: "↑ default" }, ...SPACING.map((n) => ({ value: String(n), label: `↑ ${n} pt` }))], str(s.spaceBefore)),
          (v) => v !== MIXED && setBlockAttrs(editor, { spaceBefore: numOrNull(v) }),
          "tb-space-before",
        )}
        {select(
          "Space after paragraph",
          str(s.spaceAfter),
          withMixed([{ value: "", label: "↓ default" }, ...SPACING.map((n) => ({ value: String(n), label: `↓ ${n} pt` }))], str(s.spaceAfter)),
          (v) => v !== MIXED && setBlockAttrs(editor, { spaceAfter: numOrNull(v) }),
          "tb-space-after",
        )}
        {btn("Decrease indent", () => changeIndent(editor, -1), { shortcut: "Shift+Tab in a list", children: "⇠", testid: "tb-outdent" })}
        {btn("Increase indent", () => changeIndent(editor, 1), { shortcut: "Tab in a list", children: "⇢", testid: "tb-indent" })}
      </div>

      <div className="tb-group" role="group" aria-label="Lists and links">
        {btn("Bulleted list", () => editor.chain().focus().toggleBulletList().run(), { pressed: !!s.list && !s.list.ordered, children: "•≡", testid: "tb-bullets" })}
        {btn("Numbered list", () => editor.chain().focus().toggleOrderedList().run(), { pressed: !!s.list && s.list.ordered, children: "1.", testid: "tb-numbers" })}
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
                if (!file) return;
                setUploading(true);
                try {
                  insertAssetImage(editor, await uploadImage(file));
                } catch (err) {
                  onNotice?.(err instanceof Error ? err.message : "The image could not be uploaded.");
                } finally {
                  setUploading(false);
                }
              }}
            />
          )}
          {s.image && <AltText editor={editor} initial={s.image.alt} />}
        </div>
      )}

      <div className="tb-group" role="group" aria-label="Diagram">
        {btn("Insert diagram", () => insertDiagram(editor), { children: "⎇", testid: "tb-diagram" })}
      </div>

      <div className="tb-group" role="group" aria-label="Table">
        {btn("Insert table", () => setTableOpen(!tableOpen), { pressed: tableOpen, children: "▦", testid: "tb-table" })}
        {s.table && (
          <>
            {btn("Add row above", () => tableCommand.rowAbove(editor), { children: "⤒", testid: "tb-row-above" })}
            {btn("Add row below", () => tableCommand.rowBelow(editor), { children: "⤓", testid: "tb-row-below" })}
            {btn("Add column left", () => tableCommand.colLeft(editor), { children: "⇤▮", testid: "tb-col-left" })}
            {btn("Add column right", () => tableCommand.colRight(editor), { children: "▮⇥", testid: "tb-col-right" })}
            {btn("Delete row", () => tableCommand.deleteRow(editor), { disabled: s.table.rows === 1, children: "−row", testid: "tb-del-row" })}
            {btn("Delete column", () => tableCommand.deleteColumn(editor), { disabled: s.table.cols === 1, children: "−col", testid: "tb-del-col" })}
            {btn("Delete table", () => tableCommand.deleteTable(editor), { children: "🗑", testid: "tb-del-table" })}
          </>
        )}
      </div>

      {linkOpen && <LinkForm editor={editor} initial={s.link?.href ?? ""} hasLink={!!s.link} onClose={() => onLinkOpenChange(false)} />}
      {tableOpen && <TableForm editor={editor} onClose={() => setTableOpen(false)} />}
    </div>
  );
}

function AltText(props: { editor: Editor; initial: string }) {
  const [value, setValue] = useState(props.initial);
  useEffect(() => setValue(props.initial), [props.initial]);
  const commit = () => value !== props.initial && setImageAlt(props.editor, value);
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
            props.editor.commands.focus();
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
  const [problem, setProblem] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  function apply(e: React.FormEvent) {
    e.preventDefault();
    const result = applyLink(props.editor, href.trim());
    if (result.ok) props.onClose();
    else setProblem(result.message);
  }
  const close = () => {
    props.onClose();
    props.editor.commands.focus();
  };
  return (
    <form className="tb-form" onSubmit={apply} role="group" aria-label="Link" onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), close())}>
      <label>
        Link address
        <input ref={input} type="text" value={href} onChange={(e) => setHref(e.target.value)} placeholder="https://example.com" data-testid="link-input" />
      </label>
      <button type="submit" className="primary" data-testid="link-apply">
        Apply
      </button>
      {props.hasLink && (
        <button type="button" data-testid="link-remove" onClick={() => (removeLink(props.editor), props.onClose())}>
          Remove link
        </button>
      )}
      <button type="button" onClick={close}>
        Cancel
      </button>
      {problem && (
        <span role="alert" className="error" data-testid="link-problem">
          {problem}
        </span>
      )}
    </form>
  );
}

function TableForm(props: { editor: Editor; onClose: () => void }) {
  const [rows, setRows] = useState(3);
  const [cols, setCols] = useState(3);
  const [header, setHeader] = useState(true);
  const close = () => {
    props.onClose();
    props.editor.commands.focus();
  };
  return (
    <form
      className="tb-form"
      role="group"
      aria-label="Insert table"
      onSubmit={(e) => {
        e.preventDefault();
        if (insertTable(props.editor, rows, cols, header)) props.onClose();
      }}
      onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), close())}
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
      <button type="button" onClick={close}>
        Cancel
      </button>
    </form>
  );
}
