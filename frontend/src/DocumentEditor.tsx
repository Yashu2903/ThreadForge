import type { Editor } from "@tiptap/core";
import { type PMJson, blockHash } from "@threadforge/editor-schema";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Api } from "./api";
import { CollabSession, collabUrl } from "./collab/session";
import type { MemberName } from "./collab/identity";
import { uploadImage } from "./documents/upload";
import { fontNotice } from "./editor/fonts";
import { TiptapEditor } from "./editor/TiptapEditor";
import { Toolbar } from "./editor/Toolbar";
import type { DocumentFull } from "./types";

/** What RoomView needs to know about the open document's live connection. */
export interface LiveHandle {
  docId: string;
  /** local changes the server has not acknowledged yet */
  unsynced: () => number;
  /** the document text, to copy out before it is lost */
  text: () => string;
  /** the top-level blocks the selection touches, with the hash the server will check (or null) */
  selection: () => Promise<BlockSelection | null>;
}

export interface BlockSelection {
  ids: string[];
  /** sha256 over "id:blockHash" lines of the selected blocks in document order (see capture.py) */
  hash: string;
  chars: number;
}

/** The selected top-level blocks and their selection hash. The block hashes are the shared package's
 *  `blockHash`, the same function the collaboration service uses, so both sides agree exactly. */
export async function readBlockSelection(editor: Editor | null): Promise<BlockSelection | null> {
  if (!editor) return null;
  const { from, to, empty } = editor.state.selection;
  if (empty) return null;
  const picked: { id: string; json: PMJson; chars: number }[] = [];
  editor.state.doc.forEach((node, offset) => {
    const start = offset;
    const end = offset + node.nodeSize;
    const id = node.attrs?.id;
    if (end > from && start < to && typeof id === "string") picked.push({ id, json: node.toJSON() as PMJson, chars: node.textContent.length });
  });
  if (picked.length === 0) return null;
  const lines: string[] = [];
  for (const b of picked) lines.push(`${b.id}:${await blockHash(b.json)}`);
  const bytes = new TextEncoder().encode(lines.join("\n"));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hash = [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, "0")).join("");
  return { ids: picked.map((b) => b.id), hash, chars: picked.reduce((n, b) => n + b.chars, 0) };
}

/**
 * The live document: toolbar, editor, sync indicator and the access / repair messages.
 *
 * It owns one CollabSession for the open document. The session is created in an effect and
 * destroyed in its cleanup, so opening another document, leaving the room, or unmounting always
 * closes the socket and frees the Yjs document (React StrictMode's mount/unmount/mount included).
 * The text itself never passes through React state.
 */
export function DocumentEditor(props: {
  api: Api;
  roomId: string;
  userId: string;
  doc: DocumentFull;
  members: readonly MemberName[];
  /** a phone or tablet: view only */
  mobile: boolean;
  /** the room client's current idea of this person's role (it can change while the editor is open) */
  roleCanEdit: boolean;
  onLive: (handle: LiveHandle | null) => void;
  onAtRisk: (unsynced: number) => void;
}) {
  const { api, roomId, doc } = props;
  const [session, setSession] = useState<CollabSession | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [revision, setRevision] = useState(0);
  const [linkOpen, setLinkOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [copied, setCopied] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0); // a new generation = a fresh connection and Yjs document
  const editorRef = useRef<Editor | null>(null);
  editorRef.current = editor;

  useEffect(() => {
    const created = new CollabSession({
      url: collabUrl(doc.collab_path),
      documentName: doc.document_name,
      canEdit: doc.can_edit && props.roleCanEdit && !props.mobile,
    });
    setSession(created);
    props.onLive({
      docId: doc.id,
      unsynced: () => created.unsyncedChanges,
      text: () => editorRef.current?.getText({ blockSeparator: "\n" }) ?? "",
      selection: () => readBlockSelection(editorRef.current),
    });
    return () => {
      props.onLive(null);
      props.onAtRisk(0);
      setSession(null);
      setEditor(null);
      created.destroy();
    };
    // one session per document of this room
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, roomId, doc.id, generation]);

  const idle = useCallback(() => () => undefined, []);
  const snap = useSyncExternalStore(session ? session.subscribe : idle, () => session?.getSnapshot() ?? null);

  // the permission can change while the editor is open (the room client reports role changes)
  const mayEdit = doc.can_edit && props.roleCanEdit && !props.mobile;
  useEffect(() => session?.setCanEdit(mayEdit), [session, mayEdit]);

  // tell the room view how much is at risk (for switching documents)
  useEffect(() => props.onAtRisk(snap?.atRisk ?? 0), [snap?.atRisk]); // eslint-disable-line react-hooks/exhaustive-deps

  // warn before leaving with changes the server has not acknowledged
  const atRisk = snap?.atRisk ?? 0;
  useEffect(() => {
    if (atRisk <= 0) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [atRisk]);

  async function copyMyText() {
    const text = editor?.getText({ blockSeparator: "\n" }) ?? "";
    try {
      await navigator.clipboard.writeText(text);
      setCopied("Copied to the clipboard.");
    } catch {
      setCopied(text); // no clipboard permission: show the text so it can be copied by hand
    }
  }

  const editable = !!snap && snap.editable && mayEdit;
  const font = editor ? fontNotice(editor.state.doc) : null;
  const members = props.members;

  return (
    <div className="doc-editor">
      <div className="save-bar">
        <span
          role="status"
          aria-live="polite"
          className={`save-status s-${snap?.kind ?? "connecting"}`}
          data-testid="sync-status"
          title={
            snap?.kind === "synced"
              ? "The server has received everything you typed. It is stored in the database within a few seconds."
              : snap?.kind === "offline"
                ? "You are offline. Changes stay in this tab and are sent when the connection returns. Closing the tab loses them."
                : undefined
          }
        >
          {snap?.label ?? "Connecting…"}
        </span>
        {atRisk > 0 && snap?.kind !== "access-lost" && (
          <span className="muted" data-testid="at-risk">
            {atRisk} {atRisk === 1 ? "change is" : "changes are"} not on the server yet
          </span>
        )}
      </div>

      {snap?.demoted && snap.kind !== "access-lost" && (
        <div className="conflict" role="alert" data-testid="demoted">
          <strong>You can no longer edit this document.</strong>
          <p>
            Your role in this room changed to viewer. The document keeps updating live.
            {atRisk > 0 ? " Changes you made just before this were not accepted; copy them if you need them." : ""}
          </p>
          <div className="actions">
            {atRisk > 0 && (
              <button type="button" onClick={() => void copyMyText()} data-testid="copy-text">
                Copy my text
              </button>
            )}
            <button type="button" onClick={() => setGeneration((n) => n + 1)} data-testid="reload-doc">
              Reload the document from the server
            </button>
          </div>
        </div>
      )}
      {snap?.kind === "access-lost" && (
        <div className="conflict" role="alert" data-testid="access-lost">
          <strong>
            {snap.denial === "needs_repair"
              ? "This document needs repair and cannot be opened right now."
              : "You no longer have access to this document."}
          </strong>
          {snap.denial === "needs_repair" ? (
            <p>An administrator can restore it from its last good version. Nothing you typed earlier was lost from the server.</p>
          ) : (
            <p>
              {atRisk > 0
                ? "Your last changes were not received by the server. The text is still on this page; copy it before you leave."
                : "Everything you typed before this was received by the server. The text below is a read-only copy."}
            </p>
          )}
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
      {props.mobile && (
        <p className="muted" role="note" data-testid="mobile-note">
          Editing is not supported in mobile browsers; you can view this document, and it updates live.
        </p>
      )}

      {mayEdit && (
        <Toolbar
          editor={editor}
          revision={revision}
          enabled={editable}
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
      {session && (
        <TiptapEditor
          key={session.options.documentName}
          session={session}
          editable={editable}
          label={doc.title}
          userId={props.userId}
          members={members}
          assetUrl={(id) => `/rooms/${roomId}/assets/${id}`}
          onEditor={(e) => {
            setEditor(e);
            (window as unknown as { __editor?: Editor | null }).__editor = e;
          }}
          onTransaction={() => {
            setNotice("");
            setRevision((n) => n + 1);
          }}
          onNotice={setNotice}
          onLinkRequest={() => setLinkOpen(true)}
        />
      )}
    </div>
  );
}
