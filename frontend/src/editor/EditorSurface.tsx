import { useEffect, useRef } from "react";
import type { Limits } from "threadforge-client/document-schema";
import type { Doc } from "./doc";
import { Editor } from "./editor";

/**
 * React hosts the editor; it does not render it. This component outputs ONE empty <div>. The editor
 * fills that element itself and React never renders children inside it, so a React re-render can
 * never touch (or reset) the text the user is typing. Everything dynamic goes through the editor's
 * methods, called from effects. Props that are callbacks are read through a ref, so changing them
 * does not rebuild the editor.
 */
export interface EditorSurfaceProps {
  /** The document to start with. To load a different one, call editor.setDocument() via onReady. */
  initialDocument: Doc;
  readOnly?: boolean;
  limits?: Limits;
  label?: string;
  assetUrl?: (assetId: string) => string;
  onReady?: (editor: Editor) => void;
  onChange?: (doc: Doc, info: { kind: string }) => void;
  onNotice?: (message: string) => void;
  onSave?: () => void;
}

export function EditorSurface(props: EditorSurfaceProps) {
  const host = useRef<HTMLDivElement>(null);
  const latest = useRef(props);
  latest.current = props;
  const editorRef = useRef<Editor | null>(null);

  useEffect(() => {
    const el = host.current!;
    const editor = new Editor(el, {
      document: latest.current.initialDocument,
      readOnly: latest.current.readOnly,
      limits: latest.current.limits,
      label: latest.current.label,
      assetUrl: latest.current.assetUrl,
      onChange: (doc, info) => latest.current.onChange?.(doc, info),
      onNotice: (message) => latest.current.onNotice?.(message),
      onSave: () => latest.current.onSave?.(),
    });
    editorRef.current = editor;
    latest.current.onReady?.(editor);
    return () => {
      editor.destroy();
      editorRef.current = null;
      // the editor drew into this element; leave it empty for a StrictMode remount
      el.replaceChildren();
      el.classList.remove("tf-editor");
    };
  }, []);

  useEffect(() => {
    editorRef.current?.setReadOnly(props.readOnly ?? false);
  }, [props.readOnly]);

  return <div ref={host} className="paper editor-host" data-testid="editor" />;
}
