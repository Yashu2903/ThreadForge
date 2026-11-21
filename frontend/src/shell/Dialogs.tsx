import { type ReactNode, useRef, useState } from "react";
import { type Api, ApiError } from "../api";
import { Dialog } from "../components/Dialog";
import type { RoomSummary } from "../types";
import { joinHash, parseInvitation } from "./invites";

/** Create a room: a name, one request, no double submit; the new room opens when the server has made it. */
export function CreateRoomDialog(props: { api: Api; onCreated: (room: RoomSummary) => void; onClose: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return setError("Give the room a name.");
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await props.api.send<RoomSummary>("POST", "/rooms", { name: trimmed });
      props.onCreated(res.body);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server. Try again.");
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <Dialog title="Create a room" onClose={props.onClose} busy={busy}>
      <form onSubmit={submit} className="stack">
        <p className="muted">A room holds your documents, files and chat. You are its owner and can invite people.</p>
        <label className="field">
          <span>Room name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} placeholder="Launch team" autoComplete="off" />
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={props.onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? "Creating…" : "Create room"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/**
 * Join a room: paste the key or the whole link. This only reads the key and opens the join page, which asks the server what the
 * invitation is for and joins when the person presses Join: pasting never joins by itself.
 */
export function JoinRoomDialog(props: { onClose: () => void; onOpen?: (token: string) => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const token = parseInvitation(text);
    if (!token) return setError("That does not look like an invitation key or link. Paste the whole link, or the 43-character key.");
    props.onOpen?.(token);
    props.onClose();
    window.location.hash = joinHash(token);
  }

  return (
    <Dialog title="Join a room" onClose={props.onClose}>
      <form onSubmit={submit} className="stack">
        <p className="muted">Paste the invitation link or key the room owner sent you. You will see which room it is for before you join.</p>
        <label className="field">
          <span>Invitation link or key</span>
          <input
            value={text}
            onChange={(e) => (setText(e.target.value), setError(null))}
            placeholder="Paste a link or key"
            autoComplete="off"
            spellCheck={false}
            aria-invalid={error ? true : undefined}
          />
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!text.trim()}>
            Continue
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/** A confirmation that is a real dialog (focus kept inside, Escape cancels), used instead of `window.confirm` for member removal. */
export function ConfirmDialog(props: { title: string; children: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean; error?: string | null; onConfirm: () => void; onClose: () => void }) {
  return (
    <Dialog title={props.title} onClose={props.onClose} busy={props.busy}>
      <div className="stack">
        {props.children}
        {props.error && (
          <p role="alert" className="error">
            {props.error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={props.onClose} disabled={props.busy}>
            Cancel
          </button>
          <button type="button" className={props.danger ? "danger" : "primary"} onClick={props.onConfirm} disabled={props.busy}>
            {props.busy ? "Working…" : props.confirmLabel}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
