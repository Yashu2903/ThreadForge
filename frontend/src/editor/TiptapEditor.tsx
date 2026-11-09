import { YJS_FIELD, createExtensions } from "@threadforge/editor-schema";
import type { Editor } from "@tiptap/core";
import Collaboration, { isChangeOrigin } from "@tiptap/extension-collaboration";
import CollaborationCaret from "@tiptap/extension-collaboration-caret";
import { EditorContent, useEditor } from "@tiptap/react";
import { useEffect, useMemo, useRef } from "react";
import { type MemberName, colorFor, renderCaret } from "../collab/identity";
import type { CollabSession } from "../collab/session";
import { diagramNodeView } from "./DiagramView";

/**
 * The editor. React hosts it; Tiptap owns the text. The text is never a React prop or state: it is
 * the shared Yjs document inside the CollabSession, which the Collaboration extension binds to
 * ProseMirror. Typing therefore never goes through React renders, and a re-render cannot reset it.
 *
 * What is NOT allowed to reach the document (the shared schema already rejects most of it; these
 * guards cover what a paste or drop would otherwise try):
 *   * images by URL or base64: only an `<img data-asset-id>` whose asset is ALREADY in this document
 *     survives a paste (a copy within the document); everything else is removed before parsing;
 *   * dropped/pasted image FILES are refused with an explanation (use Insert image: it uploads
 *     through the asset endpoint and stores only the asset id).
 */
export interface TiptapEditorProps {
  session: CollabSession;
  /** may the person type? (role AND server scope AND not access-lost AND not mobile) */
  editable: boolean;
  label: string;
  userId: string;
  members: readonly MemberName[];
  assetUrl: (assetId: string) => string;
  onEditor: (editor: Editor | null) => void;
  /** called after every transaction, so the toolbar re-reads the selection */
  onTransaction?: () => void;
  onNotice: (message: string) => void;
  onLinkRequest: () => void;
}

function assetIdsIn(editor: Editor): Set<string> {
  const ids = new Set<string>();
  editor.state.doc.descendants((node) => {
    if (node.type.name === "image" && node.attrs.assetId) ids.add(String(node.attrs.assetId));
  });
  return ids;
}

/** Remove every <img> that is not a copy of an image already in this document. */
export function sanitizePastedHtml(html: string, known: ReadonlySet<string>): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  for (const img of Array.from(doc.querySelectorAll("img"))) {
    const id = img.getAttribute("data-asset-id");
    if (!id || !known.has(id)) img.remove();
  }
  return doc.body.innerHTML;
}

export function TiptapEditor(props: TiptapEditorProps) {
  const { session } = props;
  const latest = useRef(props);
  latest.current = props;
  const notifying = useRef(false);
  const editorRef = useRef<Editor | null>(null);

  const extensions = useMemo(
    () => [
      ...createExtensions({
        assetUrl: (id) => latest.current.assetUrl(id),
        diagramView: diagramNodeView,
        // ids are generated for OUR transactions only; ids that arrive from collaborators are kept
        uniqueIdFilter: ((tr: unknown) => !isChangeOrigin(tr as never)) as never,
      }),
      Collaboration.configure({ document: session.ydoc, field: YJS_FIELD }),
      CollaborationCaret.configure({
        provider: session.provider,
        user: { id: props.userId, color: colorFor(props.userId) },
        render: (user: Record<string, unknown>) => renderCaret(user, latest.current.members),
      }),
    ],
    // one editor per collaboration session
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [session],
  );

  const editor = useEditor(
    {
      extensions,
      editable: props.editable,
      immediatelyRender: true,
      shouldRerenderOnTransaction: false,
      editorProps: {
        attributes: {
          role: "textbox",
          "aria-multiline": "true",
          "aria-label": props.label,
          class: "tf-editor",
          spellcheck: "true",
        },
        transformPastedHTML: (html) => {
          const ed = editorRef.current;
          return sanitizePastedHtml(html, ed ? assetIdsIn(ed) : new Set());
        },
        handlePaste: (_view, event) => {
          const files = Array.from(event.clipboardData?.files ?? []);
          if (files.some((f) => f.type.startsWith("image/"))) {
            latest.current.onNotice("Pasted pictures are not accepted. Use Insert image in the toolbar.");
            return true;
          }
          return false;
        },
        handleDrop: (_view, event) => {
          const files = Array.from(event.dataTransfer?.files ?? []);
          if (files.length > 0) {
            latest.current.onNotice("Dropped files are not accepted. Use Insert image in the toolbar.");
            return true;
          }
          return false;
        },
        handleKeyDown: (_view, event) => {
          if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
            event.preventDefault();
            latest.current.onLinkRequest();
            return true;
          }
          return false;
        },
      },
      // Tiptap can dispatch a transaction while React is still rendering this component; telling the
      // parent at once would set its state mid-render. One notification per tick, after the render.
      onTransaction: () => {
        if (notifying.current) return;
        notifying.current = true;
        queueMicrotask(() => {
          notifying.current = false;
          latest.current.onTransaction?.();
        });
      },
    },
    [session],
  );
  editorRef.current = editor;

  useEffect(() => {
    latest.current.onEditor(editor);
    return () => latest.current.onEditor(null);
  }, [editor]);

  // `editable` follows the permission and the connection state; the selection and text stay put
  useEffect(() => {
    if (editor && editor.isEditable !== props.editable) editor.setEditable(props.editable);
  }, [editor, props.editable]);
  useEffect(() => {
    editor?.view.dom.setAttribute("aria-readonly", props.editable ? "false" : "true");
    editor?.view.dom.setAttribute("aria-label", props.label);
  }, [editor, props.editable, props.label]);

  return (
    <div className="paper editor-host" data-testid="editor">
      <EditorContent editor={editor} />
    </div>
  );
}
