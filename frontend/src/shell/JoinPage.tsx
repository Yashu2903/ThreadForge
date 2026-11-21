import { useCallback, useEffect, useRef, useState } from "react";
import { type Api, ApiError } from "../api";
import type { Me, RoomSummary } from "../types";
import { IconCompass, IconUsers } from "../ui/icons";
import { HomeShell } from "./Home";
import { joinHash, parseInvitation, pendingInvitation } from "./invites";

interface Preview {
  room_name: string;
  role: string;
  expires_at: string;
  already_member: boolean;
}
interface Redeemed {
  room: RoomSummary & { membership_epoch?: number };
}

type Phase =
  | { kind: "checking" }
  | { kind: "ready"; preview: Preview }
  | { kind: "joining"; preview: Preview }
  | { kind: "invalid" }
  | { kind: "full" }
  | { kind: "network"; retry: "preview" | "join"; preview?: Preview };

const ROLE_TEXT: Record<string, string> = {
  owner: "the owner",
  editor: "an editor: you can edit documents, chat, upload files and ask the AI",
  viewer: "a viewer: you can read documents and chat, but not edit or ask the AI",
};

/**
 * Opening an invitation. The key reaches this page from the link's fragment (already removed from the address bar by the caller),
 * from the pasted key, or from the copy kept across a sign-in. Nothing is joined until the person presses Join:
 *   1. ask the server what the invitation is for (read-only preview: room name and role);
 *   2. show it, with an explicit Join (or "already a member": Open room);
 *   3. redeem (protected, idempotent for the same person), refresh the room list, open the room.
 * Every refusal (unknown, malformed, expired, revoked, already used by someone else, used by this person who has since been removed)
 * is the same "not valid" screen: the server does not say which, and neither does this page.
 */
export function JoinPage(props: { api: Api; me: Me; token: string | null }) {
  const { api } = props;
  const [token, setToken] = useState<string | null>(props.token);
  const [phase, setPhase] = useState<Phase>({ kind: "checking" });
  const [pasted, setPasted] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);
  const joining = useRef(false);

  const preview = useCallback(async (key: string) => {
    setPhase({ kind: "checking" });
    try {
      const res = await api.send<Preview>("POST", "/invitations/preview", { token: key });
      setPhase({ kind: "ready", preview: res.body });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        pendingInvitation.clear();
        setPhase({ kind: "invalid" });
      } else setPhase({ kind: "network", retry: "preview" });
    }
  }, [api]);

  useEffect(() => {
    if (token) void preview(token);
  }, [token, preview]);

  async function join(retryWith?: Preview) {
    const shown = retryWith ?? (phase.kind === "ready" ? phase.preview : null);
    if (!token || !shown || joining.current) return;
    joining.current = true;
    setPhase({ kind: "joining", preview: shown });
    try {
      const res = await api.send<Redeemed>("POST", "/invitations/redeem", { token });
      pendingInvitation.clear();
      window.location.hash = `#/rooms/${res.body.room.id}`;
    } catch (err) {
      joining.current = false;
      if (err instanceof ApiError && err.status === 404) {
        pendingInvitation.clear();
        setPhase({ kind: "invalid" });
      } else if (err instanceof ApiError && err.code === "member_limit_reached") setPhase({ kind: "full" });
      else setPhase({ kind: "network", retry: "join", preview: shown });
    }
  }

  function cancel() {
    pendingInvitation.clear();
    window.location.hash = "#/";
  }

  function submitPaste(event: React.FormEvent) {
    event.preventDefault();
    const parsed = parseInvitation(pasted);
    if (!parsed) return setPasteError("That does not look like an invitation key or link. Paste the whole link, or the 43-character key.");
    pendingInvitation.save(parsed);
    setPasteError(null);
    setPasted("");
    setToken(parsed);
  }

  return (
    <HomeShell api={api} me={props.me} active="join">
      <div className="page narrow">
        <div className="join-card" data-testid="join-page">
          {!token && (
            <form onSubmit={submitPaste} className="stack">
              <h1>Join a room</h1>
              <p className="muted">Paste the invitation link or key the room owner sent you.</p>
              <label className="field">
                <span>Invitation link or key</span>
                <input value={pasted} onChange={(e) => (setPasted(e.target.value), setPasteError(null))} placeholder="Paste a link or key" autoComplete="off" spellCheck={false} autoFocus />
              </label>
              {pasteError && (
                <p role="alert" className="error">
                  {pasteError}
                </p>
              )}
              <div className="actions">
                <button type="button" onClick={cancel}>
                  Cancel
                </button>
                <button type="submit" className="primary" disabled={!pasted.trim()}>
                  Continue
                </button>
              </div>
            </form>
          )}
          {token && phase.kind === "checking" && (
            <p role="status" className="muted">
              Checking the invitation…
            </p>
          )}
          {token && (phase.kind === "ready" || phase.kind === "joining") && (
            <div className="stack">
              <span className="join-icon" aria-hidden="true">
                <IconUsers size={28} />
              </span>
              {phase.preview.already_member ? (
                <>
                  <h1>You are already in {phase.preview.room_name}</h1>
                  <p className="muted">This invitation is not needed: you are a member of this room.</p>
                </>
              ) : (
                <>
                  <h1>Join {phase.preview.room_name}?</h1>
                  <p>
                    You were invited as <strong>{ROLE_TEXT[phase.preview.role] ?? phase.preview.role}</strong>.
                  </p>
                  <p className="muted small">This invitation expires {new Date(phase.preview.expires_at).toLocaleString()} and works once.</p>
                </>
              )}
              <div className="actions">
                <button type="button" onClick={cancel} disabled={phase.kind === "joining"}>
                  {phase.preview.already_member ? "Back" : "Cancel"}
                </button>
                <button type="button" className="primary" onClick={() => void join()} disabled={phase.kind === "joining"} data-testid="join-room">
                  {phase.kind === "joining" ? "Joining…" : phase.preview.already_member ? "Open room" : "Join room"}
                </button>
              </div>
            </div>
          )}
          {token && phase.kind === "invalid" && (
            <div className="stack" role="alert" data-testid="invitation-invalid">
              <h1>This invitation is not valid</h1>
              <p className="muted">It may have expired, been revoked, or already been used. Ask the room owner for a new invitation.</p>
              <div className="actions">
                <button type="button" onClick={() => (setToken(null), window.history.replaceState(null, "", joinHash()))}>
                  <IconCompass size={16} /> Try another key
                </button>
                <button type="button" className="primary" onClick={cancel}>
                  Back to your rooms
                </button>
              </div>
            </div>
          )}
          {token && phase.kind === "full" && (
            <div className="stack" role="alert">
              <h1>This room is full</h1>
              <p className="muted">The room has reached its member limit. Your invitation was not used; ask the owner to make space.</p>
              <div className="actions">
                <button type="button" className="primary" onClick={cancel}>
                  Back to your rooms
                </button>
              </div>
            </div>
          )}
          {token && phase.kind === "network" && (
            <div className="stack" role="alert">
              <h1>Could not reach the server</h1>
              <p className="muted">{phase.retry === "join" ? "You have not joined yet. It is safe to try again: the invitation works once, and trying again never adds you twice." : "The invitation could not be checked."}</p>
              <div className="actions">
                <button type="button" onClick={cancel}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="primary"
                  onClick={() => (phase.retry === "join" && phase.preview ? void join(phase.preview) : token && void preview(token))}
                >
                  Try again
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </HomeShell>
  );
}
