import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { Api } from "./api";
import { Dialog } from "./components/Dialog";
import { DocumentSession, type ReadOnlyReason, type SaveStatus } from "./documents/session";
import type { DocumentStore } from "./documents/store";
import { uploadImage } from "./documents/upload";
import type { Doc } from "./editor/doc";
import type { Editor } from "./editor/editor";
import { EditorSurface } from "./editor/EditorSurface";
import { fontNotice } from "./editor/fonts";
import { Toolbar } from "./editor/Toolbar";
import type { DocumentFull } from "./types";

const LABELS: Record<SaveStatus, string> = {
  saved: "Saved",
  unsaved: "Unsaved changes",
  saving: "Saving…",
  failed: "Save failed",
  conflict: "Conflict",
  "read-only": "Read-only",
};

/**
 * The editable document: toolbar, editor, save status, and the conflict / failure / access-loss
 * messages. The DocumentSession owns every saving rule (see documents/session.ts); this component
 * only shows its state and passes the user's choices to it.
 */
export function DocumentEditor(props: {
  api: Api;
  roomId: string;
  doc: DocumentFull;
  /** the newest copy the document store has fetched from the server */
  serverDoc: DocumentFull | null;
  store: DocumentStore;
  readOnlyReason: ReadOnlyReason | null;
  onSession: (session: DocumentSession) => void;
}) {
  const { api, roomId, doc, store } = props;
  const session = useMemo(
    () => new DocumentSession({ api, roomId, initial: doc }),
    // one session per document; the store refetching the same document must not make a new one
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [api, roomId, doc.id],
  );
  const snap = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [revision, setRevision] = useState(0);
  const [linkOpen, setLinkOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [confirmLoad, setConfirmLoad] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const initial = useRef(doc.content as unknown as Doc);

  useEffect(() => {
    session.activate();
    props.onSession(session);
    return () => session.dispose();
  }, [session]); // eslint-disable-line react-hooks/exhaustive-deps

  // events about this document, straight from the room client (before its cursor advances)
  useEffect(() => store.onContentEvent((id, version) => id === doc.id && session.onServerEvent(version)), [store, session, doc.id]);

  // the store fetched a newer copy: a clean session adopts it, a dirty one reports a conflict
  useEffect(() => {
    if (props.serverDoc) session.onServerDocument(props.serverDoc);
  }, [props.serverDoc?.content_version, session]); // eslint-disable-line react-hooks/exhaustive-deps

  // losing (or regaining) edit rights
  useEffect(() => session.setReadOnly(props.readOnlyReason), [props.readOnlyReason, session]);

  // warn before leaving with anything unsaved
  const risky = snap.dirty || snap.status === "saving" || snap.status === "failed" || snap.status === "conflict";
  useEffect(() => {
    if (!risky) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [risky]);

  async function loadLatest() {
    setConfirmLoad(false);
    try {
      const fresh = await api.get<DocumentFull>(`/rooms/${roomId}/documents/${doc.id}`);
      session.loadLatest(fresh.body);
    } catch {
      setNotice("Could not load the latest version. Your changes are still here.");
    }
  }

  async function copyMyText() {
    const text = session.localText();
    try {
      await navigator.clipboard.writeText(text);
      setCopied("Copied to the clipboard.");
    } catch {
      // no clipboard permission: show the text so it can be copied by hand
      setCopied(text);
    }
  }

  const readOnly = snap.readOnlyReason !== null;
  const canSave = !readOnly && snap.status !== "conflict" && (snap.dirty || snap.status === "failed");
  const font = fontNotice(editor?.getDocument() ?? (doc.content as unknown as Doc));

  return (
    <div className="doc-editor">
      <div className="save-bar">
        <span role="status" aria-live="polite" className={`save-status s-${snap.status}`} data-testid="save-status">
          {LABELS[snap.status]}
        </span>
        <span className="muted" data-testid="doc-version">
          Version {snap.contentVersion}
        </span>
        {!readOnly && (
          <button type="button" onClick={() => void session.saveNow()} disabled={!canSave && snap.status !== "saving"} data-testid="save-button">
            Save
          </button>
        )}
        {snap.status === "failed" && (
          <button type="button" onClick={() => session.retryNow()} data-testid="retry-button">
            Retry now
          </button>
        )}
      </div>

      {snap.message && snap.status !== "conflict" && (
        <p className={snap.status === "failed" ? "error" : "muted"} role={snap.status === "failed" ? "alert" : undefined} data-testid="save-message">
          {snap.message}
        </p>
      )}

      {snap.status === "conflict" && (
        <div className="conflict" role="alert" data-testid="conflict">
          <strong>Another session saved a newer version (version {snap.serverVersion ?? "?"}).</strong>
          <p>Your changes have not been saved, and nothing was overwritten. Your text is still here and you can keep editing it.</p>
          <div className="actions">
            <button type="button" onClick={() => void copyMyText()} data-testid="copy-text">
              Copy my text
            </button>
            <button type="button" onClick={() => setConfirmLoad(true)} data-testid="load-latest">
              Load latest…
            </button>
          </div>
        </div>
      )}

      {snap.readOnlyReason && snap.dirty && (
        <div className="conflict" role="alert" data-testid="readonly-unsaved">
          <p>Your unsaved changes are still on this page. Copy them before you leave.</p>
          <button type="button" onClick={() => void copyMyText()} data-testid="copy-text">
            Copy my text
          </button>
        </div>
      )}
      {copied && (
        <p role="status" data-testid="copied">
          {copied.length > 40 || copied.includes("\n") ? <textarea readOnly rows={6} value={copied} aria-label="Your text" /> : copied}
        </p>
      )}
      {notice && (
        <p role="status" className="muted" data-testid="editor-notice">
          {notice}
        </p>
      )}

      {!readOnly && (
        <Toolbar
          editor={editor}
          revision={revision}
          linkOpen={linkOpen}
          onLinkOpenChange={setLinkOpen}
          onNotice={setNotice}
          uploadImage={(file) => uploadImage(api, roomId, file)}
        />
      )}
      {font && (
        <p className="font-notice" role="note" data-testid="font-notice">
          {font}
        </p>
      )}
      <EditorSurface
        initialDocument={initial.current}
        readOnly={readOnly}
        label={doc.title}
        assetUrl={(id) => `/rooms/${roomId}/assets/${id}`}
        onReady={(e) => {
          setEditor(e);
          session.attach(e);
          (window as unknown as { __editor?: Editor }).__editor = e;
        }}
        onChange={() => {
          setNotice("");
          session.onEditorChange();
        }}
        onNotice={setNotice}
        onSave={() => void session.saveNow()}
        onLinkRequest={() => setLinkOpen(true)}
        onStateChange={() => setRevision((n) => n + 1)}
      />

      {confirmLoad && (
        <Dialog title="Load the latest version?" onClose={() => setConfirmLoad(false)}>
          <p>
            This replaces the document on your screen with the version saved by the other session. <strong>Your unsaved changes will be discarded.</strong>{" "}
            You can copy your text first.
          </p>
          <div className="actions">
            <button type="button" onClick={() => setConfirmLoad(false)}>
              Keep my changes
            </button>
            <button type="button" className="primary" onClick={() => void loadLatest()} data-testid="confirm-load-latest">
              Discard my changes and load
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
