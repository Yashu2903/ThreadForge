import { useEffect, useRef, useState } from "react";
import { type Api, ApiError } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { DocumentRead } from "./components/DocumentRead";
import { OpenDialog } from "./components/OpenDialog";
import { StartDialog } from "./components/StartDialog";
import type { Capabilities, DocumentFull, Me } from "./types";
import { useClientState, useRoomHandles, useStore } from "./useRoom";

/**
 * The room: navigation (documents) on the left, the document area in the middle, chat on the right.
 * One RoomClient feeds both the chat and the document events; the DocumentStore holds the list and
 * the open document. A clean client follows the room's active document when the owner changes it.
 */
export function RoomView(props: { api: Api; me: Me; roomId: string; onBack: () => void }) {
  const { api, me, roomId } = props;
  const { client, store } = useRoomHandles(api, roomId, me.id);
  const state = useClientState(client);
  const docs = useStore(store);
  const [dialog, setDialog] = useState<"start" | "open" | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const role = state.role;
  const isOwner = role === "owner";
  const activeId = state.room?.active_document_id ?? null;

  useEffect(() => {
    let cancelled = false;
    api
      .get<Capabilities>("/capabilities")
      .then((res) => !cancelled && setCapabilities(res.body))
      .catch(() => undefined); // the Drive explanation simply does not show
    return () => {
      cancelled = true;
    };
  }, [api]);

  // Follow the room's active document (this client has no unsaved work to protect yet).
  const followed = useRef<string | null>(null);
  useEffect(() => {
    if (activeId && followed.current !== activeId) {
      followed.current = activeId;
      void store.open(activeId);
    }
  }, [activeId, store]);

  if (state.status === "terminal" && state.terminalReason === "access_lost") {
    return (
      <main className="centered">
        <h1>You no longer have access to this room</h1>
        <button onClick={props.onBack}>Back to your rooms</button>
      </main>
    );
  }
  if (state.status === "terminal" && state.terminalReason === "sign_in_required") {
    return (
      <main className="centered">
        <h1>Your session ended</h1>
        <a href="/auth/login">Sign in again</a>
      </main>
    );
  }

  async function makeActive(id: string) {
    setNotice(null);
    try {
      await api.send("PUT", `/rooms/${roomId}/active-document`, {
        document_id: id,
        expected_selection_version: state.room?.active_document_version,
      });
      setDialog(null);
    } catch (err) {
      setNotice(
        err instanceof ApiError && err.code === "selection_conflict"
          ? "The active document changed while you were choosing. Look at the list again."
          : err instanceof ApiError
            ? err.message
            : "Could not reach the server.",
      );
    }
  }

  function created(doc: DocumentFull) {
    setDialog(null);
    followed.current = doc.id;
    void store.open(doc.id);
    void store.loadList();
  }

  const open = docs.open;
  const openDoc = open?.doc ?? null;

  return (
    <div className="room-layout">
      <nav className="sidebar" aria-label="Room navigation">
        <button className="link" onClick={props.onBack}>
          ← Rooms
        </button>
        <h1 title={state.room?.name}>{state.room?.name ?? "Loading…"}</h1>
        <p className="muted">
          {me.display_name} · {role ?? "…"}
        </p>
        <h2>Documents</h2>
        {isOwner && (
          <div className="stack">
            <button className="primary" onClick={() => setDialog("start")}>
              Start document
            </button>
          </div>
        )}
        <button onClick={() => setDialog("open")}>Open document</button>
        <ul className="doc-nav" aria-label="Documents in this room">
          {docs.list.items.map((doc) => (
            <li key={doc.id}>
              <button
                className={doc.id === open?.id ? "selected" : ""}
                aria-current={doc.id === open?.id ? "true" : undefined}
                onClick={() => void store.open(doc.id)}
              >
                {doc.title}
              </button>
              {doc.is_active && <span className="badge">Active</span>}
            </li>
          ))}
        </ul>
      </nav>

      <main className="document-area" aria-label="Document">
        {notice && (
          <p role="alert" className="error">
            {notice}
          </p>
        )}
        {!open && (
          <section className="empty">
            <h2>{docs.list.items.length === 0 ? "No document yet" : "No document open"}</h2>
            {isOwner ? (
              <>
                <p>Start a new document, or open a saved one.</p>
                <div className="actions">
                  <button className="primary" onClick={() => setDialog("start")}>
                    Start document
                  </button>
                  <button onClick={() => setDialog("open")}>Open document</button>
                </div>
              </>
            ) : (
              <p>The room owner has not chosen a document yet. It will appear here when they do.</p>
            )}
          </section>
        )}
        {open && (
          <>
            {open.loading && !openDoc && <p role="status">Loading document…</p>}
            {open.notFound && <p role="alert">This document is not available.</p>}
            {open.error && (
              <p role="alert" className="error">
                {open.error}
              </p>
            )}
            {openDoc && (
              <>
                <header className="doc-header">
                  <Title api={api} doc={openDoc} isOwner={isOwner} onRenamed={() => void store.open(openDoc.id)} />
                  <div className="doc-meta">
                    {openDoc.is_active ? <span className="badge">Active document</span> : <span className="badge muted-badge">Not the active document</span>}
                    {isOwner && !openDoc.is_active && (
                      <button onClick={() => void makeActive(openDoc.id)}>Make active</button>
                    )}
                    <span data-testid="access-mode">{openDoc.can_edit ? "Access: can edit" : "Access: read-only"}</span>
                    <span className="muted" data-testid="save-status">
                      Version {openDoc.content_version}
                    </span>
                  </div>
                </header>
                <DocumentRead content={openDoc.content} roomId={roomId} />
              </>
            )}
          </>
        )}
      </main>

      <ChatPanel client={client} userId={me.id} />

      {dialog === "start" && <StartDialog api={api} roomId={roomId} onCreated={created} onClose={() => setDialog(null)} />}
      {dialog === "open" && (
        <OpenDialog
          store={store}
          isOwner={isOwner}
          capabilities={capabilities}
          onOpen={(id) => {
            setDialog(null);
            void store.open(id);
          }}
          onMakeActive={makeActive}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}

function Title(props: { api: Api; doc: DocumentFull; isOwner: boolean; onRenamed: () => void }) {
  const { api, doc } = props;
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(doc.title);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setValue(doc.title), [doc.title]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const title = value.trim();
    if (!title || title === doc.title) return setEditing(false);
    try {
      await api.send("PATCH", `/rooms/${doc.room_id}/documents/${doc.id}`, { title });
      setEditing(false);
      setError(null);
      props.onRenamed();
    } catch (err) {
      setError(err instanceof ApiError ? "That title is not valid." : "Could not reach the server.");
    }
  }

  if (!editing) {
    return (
      <div className="title-row">
        <h2>{doc.title}</h2>
        {props.isOwner && <button onClick={() => setEditing(true)}>Rename</button>}
      </div>
    );
  }
  return (
    <form className="title-row" onSubmit={save}>
      <label className="sr-only" htmlFor="doc-title">
        Document title
      </label>
      <input id="doc-title" value={value} onChange={(e) => setValue(e.target.value)} maxLength={400} autoFocus />
      <button type="submit" className="primary">
        Save title
      </button>
      <button type="button" onClick={() => setEditing(false)}>
        Cancel
      </button>
      {error && (
        <span role="alert" className="error">
          {error}
        </span>
      )}
    </form>
  );
}
