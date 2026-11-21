import type { Api } from "../api";
import { colourFor, initialOf } from "../components/RoomRail";
import type { Me } from "../types";
import { Menu } from "../ui/Popover";
import { IconLogout, IconMore } from "../ui/icons";

/** The account area at the bottom of the sidebar (home and rooms alike): who you are, what you are here, and signing out. */
export function UserPanel(props: { api: Api; me: Me; detail?: string; presence?: "online" | "away" }) {
  const { api, me } = props;
  async function signOut() {
    try {
      await api.send("POST", "/auth/logout");
    } catch {
      /* repeating logout is safe; the reload below shows the signed-out page either way */
    }
    window.location.hash = "#/";
    window.location.reload();
  }
  return (
    <div className="user-panel" data-testid="user-panel">
      <span className="avatar-wrap">
        <span className="avatar" style={{ background: colourFor(me.id) }} data-initial={initialOf(me.display_name)} aria-hidden="true" />
        <span className={`presence-dot ${props.presence ?? "online"}`} aria-hidden="true" />
      </span>
      <span className="user-text">
        <strong title={me.display_name}>{me.display_name}</strong>
        <small className="muted">{props.detail ?? "Online"}</small>
      </span>
      <Menu
        label="Account"
        align="end"
        triggerClassName="icon-button"
        trigger={<IconMore />}
        items={[{ id: "out", label: "Sign out", icon: <IconLogout />, onSelect: () => void signOut() }]}
      />
    </div>
  );
}
