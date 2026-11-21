import { useRef, useState } from "react";
import { type Api, ApiError } from "../api";
import { Dialog } from "../components/Dialog";
import { IconCheck, IconCopy } from "../ui/icons";

export interface CreatedInvitation {
  id: string;
  room_id: string;
  role: "editor" | "viewer";
  expires_at: string;
  token: string;
  url: string;
}

/** The lifetimes offered (hours). The server accepts 1 to 168 by default; if a deployment narrows that, its message is shown. */
export const EXPIRY_CHOICES = [
  { hours: 1, label: "1 hour" },
  { hours: 24, label: "24 hours" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "7 days" },
] as const;

/** Copy text; when the browser refuses (no permission, insecure context), say so and let the person select it by hand. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * Invite people: choose Editor or Viewer and how long the invitation lasts, generate it, copy the link or the key.
 * One invitation is for ONE person (it works once); to invite another person, generate another. The raw key exists only in the
 * answer to the creation request: this dialog is the only place it is shown, and closing the dialog forgets it.
 */
export function InviteDialog(props: { api: Api; roomId: string; roomName: string; onCreated: () => void; onClose: () => void }) {
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [hours, setHours] = useState(24);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedInvitation | null>(null);
  const [copied, setCopied] = useState<"link" | "key" | null>(null);
  const [manual, setManual] = useState<"link" | "key" | null>(null);
  const inFlight = useRef(false);
  const linkBox = useRef<HTMLInputElement>(null);
  const keyBox = useRef<HTMLInputElement>(null);

  async function generate(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return; // one click, one invitation
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await props.api.send<CreatedInvitation>("POST", `/rooms/${props.roomId}/invitations`, { role, expires_in_hours: hours });
      setCreated(res.body);
      props.onCreated();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.code === "invitation_limit_reached"
            ? "This room has too many active invitations. Revoke one you no longer need, then try again."
            : err.status === 403 || err.status === 404
              ? "Only the room owner can invite people."
              : err.message
          : "Could not reach the server. Nothing was created; try again.",
      );
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  async function copy(what: "link" | "key") {
    if (!created) return;
    const ok = await copyText(what === "link" ? created.url : created.token);
    setManual(ok ? null : what);
    setCopied(ok ? what : null);
    if (!ok) {
      // the browser refused: leave the text selected in its box so Ctrl+C works
      const box = what === "link" ? linkBox.current : keyBox.current;
      box?.focus();
      box?.select();
    }
    if (ok) window.setTimeout(() => setCopied((c) => (c === what ? null : c)), 2500);
  }

  function another() {
    setCreated(null);
    setCopied(null);
    setManual(null);
    setError(null);
  }

  return (
    <Dialog title="Invite people" onClose={props.onClose} busy={busy}>
      {!created ? (
        <form onSubmit={generate} className="stack">
          <p className="muted">
            Invite someone to <strong>{props.roomName}</strong>. They need an account; the link lets them join this room only.
          </p>
          <fieldset className="field choice-group">
            <legend>They can</legend>
            <label className="choice">
              <input type="radio" name="invite-role" checked={role === "editor"} onChange={() => setRole("editor")} />
              <span>
                Editor<small>Edit documents, chat, upload files and ask the AI.</small>
              </span>
            </label>
            <label className="choice">
              <input type="radio" name="invite-role" checked={role === "viewer"} onChange={() => setRole("viewer")} />
              <span>
                Viewer<small>Read documents and chat. They cannot edit or ask the AI.</small>
              </span>
            </label>
          </fieldset>
          <label className="field">
            <span>Expires after</span>
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {EXPIRY_CHOICES.map((c) => (
                <option key={c.hours} value={c.hours}>
                  {c.label}
                </option>
              ))}
            </select>
          </label>
          <p className="hint notice">Each invitation works for one person, once. To invite someone else, generate another.</p>
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
              {busy ? "Generating…" : "Generate invitation"}
            </button>
          </div>
        </form>
      ) : (
        <div className="stack" data-testid="invitation-created">
          <p>
            Invitation created for an <strong>{created.role}</strong>. It works once and expires {new Date(created.expires_at).toLocaleString()}.
          </p>
          <div className="copy-row">
            <label className="field grow">
              <span>Invitation link</span>
              <input ref={linkBox} readOnly value={created.url} onFocus={(e) => e.currentTarget.select()} data-testid="invite-link" />
            </label>
            <button type="button" className="primary" onClick={() => void copy("link")} data-testid="copy-link">
              {copied === "link" ? <IconCheck /> : <IconCopy />}
              {copied === "link" ? "Copied" : "Copy link"}
            </button>
          </div>
          <div className="copy-row">
            <label className="field grow">
              <span>Invitation key</span>
              <input ref={keyBox} readOnly value={created.token} onFocus={(e) => e.currentTarget.select()} data-testid="invite-key" />
            </label>
            <button type="button" onClick={() => void copy("key")} data-testid="copy-key">
              {copied === "key" ? <IconCheck /> : <IconCopy />}
              {copied === "key" ? "Copied" : "Copy key"}
            </button>
          </div>
          {manual && (
            <p role="alert" className="error">
              Your browser would not let this page copy the {manual}. It is selected above: press Ctrl+C (Cmd+C on a Mac).
            </p>
          )}
          <p className="hint">This is the only time the key is shown. If you lose it, revoke the invitation in Members and create a new one.</p>
          <div className="actions">
            <button type="button" onClick={another}>
              Invite someone else
            </button>
            <button type="button" className="primary" onClick={props.onClose}>
              Done
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
