import { useCallback, useEffect, useRef, useState } from "react";
import type { Member } from "threadforge-client";
import { type Api, ApiError } from "../api";
import { colourFor, initialOf } from "../components/RoomRail";
import type { Page } from "../types";
import { IconClose, IconUserPlus } from "../ui/icons";
import { ConfirmDialog } from "./Dialogs";

interface InvitationRow {
  id: string;
  role: "editor" | "viewer";
  created_at: string;
  expires_at: string;
  used_at: string | null;
  used_by: string | null;
  revoked_at: string | null;
  status: "active" | "used" | "revoked" | "expired";
}

const ROLE_LABEL: Record<string, string> = { owner: "Owner", editor: "Editor", viewer: "Viewer" };
const STATUS_LABEL: Record<InvitationRow["status"], string> = { active: "Active", used: "Used", revoked: "Revoked", expired: "Expired" };

function fail(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.code === "owner_protected") return "The owner cannot be removed or changed.";
    if (err.status === 403 || err.status === 404) return "You are not allowed to do that, or the person is no longer in this room.";
    return err.message;
  }
  return fallback;
}

/**
 * The members drawer: who is in the room (from the room's own live list, with presence), and for the owner the controls to change a
 * role or remove someone, and the room's invitations. It is a drawer on top of the chat column, not a fourth permanent column, so
 * the editor keeps its width. Every control here calls an owner-only route; the server decides, whatever this page shows.
 */
export function MembersPanel(props: {
  api: Api;
  roomId: string;
  meId: string;
  role: string | null;
  members: readonly Member[];
  online: ReadonlySet<string>;
  refreshKey: number;
  onInvite: () => void;
  onClose: () => void;
}) {
  const { api, roomId } = props;
  const isOwner = props.role === "owner";
  const [tab, setTab] = useState<"members" | "invitations">("members");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Member | null>(null);
  const [invitations, setInvitations] = useState<InvitationRow[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const loadInvitations = useCallback(async () => {
    if (!isOwner) return;
    try {
      const res = await api.get<Page<InvitationRow>>(`/rooms/${roomId}/invitations?limit=50`);
      setInvitations(res.body.items);
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
    }
  }, [api, roomId, isOwner]);

  // reload when the drawer opens, when an invitation was just created, and when the member list changes (someone used one)
  useEffect(() => {
    void loadInvitations();
  }, [loadInvitations, props.refreshKey, props.members.length]);

  async function changeRole(member: Member, role: "editor" | "viewer") {
    setError(null);
    setBusy(member.user_id);
    try {
      await api.send("PATCH", `/rooms/${roomId}/members/${member.user_id}`, { role });
    } catch (err) {
      setError(fail(err, "Could not reach the server. The role was not changed."));
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    if (!removing) return;
    setError(null);
    setBusy(removing.user_id);
    try {
      await api.send("DELETE", `/rooms/${roomId}/members/${removing.user_id}`);
      setRemoving(null);
    } catch (err) {
      setError(fail(err, "Could not reach the server. Nobody was removed."));
    } finally {
      setBusy(null);
    }
  }

  async function revoke(row: InvitationRow) {
    setError(null);
    setBusy(row.id);
    try {
      await api.send("DELETE", `/rooms/${roomId}/invitations/${row.id}`);
      await loadInvitations();
    } catch (err) {
      setError(err instanceof ApiError && err.code === "invitation_already_redeemed" ? "That invitation was already used." : fail(err, "Could not reach the server."));
      void loadInvitations();
    } finally {
      setBusy(null);
    }
  }

  const names = new Map(props.members.map((m) => [m.user_id, m.display_name]));
  const groups = (["owner", "editor", "viewer"] as const)
    .map((r) => ({ role: r, items: props.members.filter((m) => m.role === r) }))
    .filter((g) => g.items.length > 0);

  return (
    <aside className="drawer members-drawer" aria-label="Members" data-testid="members-drawer">
      <header className="drawer-header">
        <h2>Members</h2>
        <span className="muted small">{props.members.length}</span>
        <span className="spacer" />
        {isOwner && (
          <button type="button" className="primary compact" onClick={props.onInvite}>
            <IconUserPlus size={16} /> Invite people
          </button>
        )}
        <button ref={closeRef} type="button" className="icon-button" aria-label="Close members" onClick={props.onClose}>
          <IconClose />
        </button>
      </header>
      {isOwner && (
        <div className="tabs" role="tablist" aria-label="Members and invitations">
          <button type="button" role="tab" aria-selected={tab === "members"} className={tab === "members" ? "active" : ""} onClick={() => setTab("members")}>
            People
          </button>
          <button type="button" role="tab" aria-selected={tab === "invitations"} className={tab === "invitations" ? "active" : ""} onClick={() => setTab("invitations")}>
            Invitations
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="error drawer-error">
          {error}
        </p>
      )}
      <div className="drawer-body">
        {tab === "members" && (
          <>
            {groups.map((g) => (
              <section key={g.role} className="member-group" aria-label={`${ROLE_LABEL[g.role]}s`}>
                <h3 className="category">
                  {ROLE_LABEL[g.role]}
                  {g.role === "owner" ? "" : "s"} — {g.items.length}
                </h3>
                <ul>
                  {g.items.map((m) => {
                    const me = m.user_id === props.meId;
                    const online = props.online.has(m.user_id);
                    return (
                      <li key={m.user_id} data-testid={`member-${m.user_id}`}>
                        <span className="avatar-wrap">
                          <span className="avatar" style={{ background: colourFor(m.user_id) }} data-initial={initialOf(m.display_name)} aria-hidden="true" />
                          <span className={`presence-dot ${online ? "online" : "offline"}`} aria-hidden="true" />
                        </span>
                        <span className="member-text">
                          <span className="member-name" title={m.display_name}>
                            {m.display_name}
                            {me && <small className="muted"> (you)</small>}
                          </span>
                          <small className="muted">{online ? "Online" : "Offline"}</small>
                        </span>
                        {isOwner && m.role !== "owner" ? (
                          <span className="member-actions">
                            <select
                              aria-label={`Role of ${m.display_name}`}
                              value={m.role}
                              disabled={busy === m.user_id}
                              onChange={(e) => void changeRole(m, e.target.value as "editor" | "viewer")}
                            >
                              <option value="editor">Editor</option>
                              <option value="viewer">Viewer</option>
                            </select>
                            <button type="button" className="icon-button danger-hover" aria-label={`Remove ${m.display_name}`} disabled={busy === m.user_id} onClick={() => setRemoving(m)}>
                              <IconClose />
                            </button>
                          </span>
                        ) : (
                          <span className="role-chip">{ROLE_LABEL[m.role] ?? m.role}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
            {props.members.length === 0 && <p className="muted">Loading members…</p>}
          </>
        )}
        {tab === "invitations" && isOwner && (
          <section aria-label="Invitations">
            <p className="hint">Each invitation works for one person, once. To invite someone else, create another.</p>
            {loadFailed && <p className="error" role="alert">The invitations could not be loaded.</p>}
            {invitations === null && !loadFailed && <p className="muted">Loading…</p>}
            {invitations?.length === 0 && <p className="muted">No invitations yet. Use “Invite people” to create one.</p>}
            <ul className="invitation-list">
              {invitations?.map((row) => (
                <li key={row.id} data-testid={`invitation-${row.status}`}>
                  <span className={`status-pill s-${row.status}`}>{STATUS_LABEL[row.status]}</span>
                  <span className="invitation-text">
                    <strong>{ROLE_LABEL[row.role]}</strong>
                    <small className="muted">
                      {row.status === "used" && row.used_by ? `Used by ${names.get(row.used_by) ?? "someone"} · ` : ""}
                      created {new Date(row.created_at).toLocaleString()} · {row.status === "expired" ? "expired" : "expires"} {new Date(row.expires_at).toLocaleString()}
                    </small>
                  </span>
                  {row.status === "active" && (
                    <button type="button" className="compact" disabled={busy === row.id} onClick={() => void revoke(row)}>
                      Revoke
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
      {removing && (
        <ConfirmDialog title="Remove from room" confirmLabel="Remove" danger busy={busy === removing.user_id} error={error} onConfirm={() => void remove()} onClose={() => setRemoving(null)}>
          <p>
            Remove <strong>{removing.display_name}</strong> from this room? They lose access to its documents, files and chat at once. To let them back in, they need a new invitation.
          </p>
        </ConfirmDialog>
      )}
    </aside>
  );
}
