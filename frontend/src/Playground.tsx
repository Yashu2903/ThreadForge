import { useMemo, useRef, useState } from "react";
import { Api } from "./api";
import { uploadImage } from "./documents/upload";
import { canonicalJson, type Json } from "threadforge-client/document-schema";
import { EditorSurface } from "./editor/EditorSurface";
import { Toolbar } from "./editor/Toolbar";
import type { Editor } from "./editor/editor";
import type { Doc } from "./editor/doc";

/**
 * DEVELOPMENT ONLY (#/playground, never routed in a production build): the editor on its own, with
 * the canonical JSON of the current document shown beside it. It exists so the editor can be driven
 * and inspected in a real browser (Playwright) before it is wired to saving. It saves nothing.
 */
const START: Doc = {
  schema_version: 1,
  blocks: [
    { type: "heading", id: "a0000000-0000-4000-8000-000000000001", level: 1, children: [{ type: "text", text: "Playground" }] },
    { type: "paragraph", id: "a0000000-0000-4000-8000-000000000002", children: [{ type: "text", text: "Type here." }] },
    { type: "paragraph", id: "a0000000-0000-4000-8000-000000000003" },
  ],
};

export function Playground({ roomId }: { roomId?: string }) {
  const api = useMemo(() => new Api(), []);
  const editorRef = useRef<Editor | null>(null);
  const [json, setJson] = useState(() => canonicalJson(START as unknown as Json));
  const [notice, setNotice] = useState("");
  const [renders, setRenders] = useState(0);
  const [revision, setRevision] = useState(0);
  const [linkOpen, setLinkOpen] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null);

  return (
    <main className="playground">
      <h1>Editor playground</h1>
      <div className="row-actions">
        <button onClick={() => setRenders((n) => n + 1)}>Re-render React ({renders})</button>
      </div>
      <Toolbar
        editor={editor}
        revision={revision}
        linkOpen={linkOpen}
        onLinkOpenChange={setLinkOpen}
        onNotice={setNotice}
        uploadImage={roomId ? (file) => uploadImage(api, roomId, file) : undefined}
      />
      <p role="status" data-testid="notice">
        {notice}
      </p>
      <EditorSurface
        initialDocument={START}
        assetUrl={roomId ? (id) => `/rooms/${roomId}/assets/${id}` : undefined}
        onReady={(editor) => {
          editorRef.current = editor;
          setEditor(editor);
          (window as unknown as { __editor: Editor }).__editor = editor;
        }}
        onStateChange={() => setRevision((n) => n + 1)}
        onLinkRequest={() => setLinkOpen(true)}
        onChange={(doc) => setJson(canonicalJson(doc as unknown as Json))}
        onNotice={setNotice}
      />
      <pre data-testid="json" className="muted">
        {json}
      </pre>
    </main>
  );
}
