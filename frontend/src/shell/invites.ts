/**
 * Invitation keys and links, and keeping an invitation across a sign-in.
 *
 * A key is the 43 URL-safe characters the server hands out once. A link is `{app}/#/join/<key>` (the app uses hash routes, and a
 * fragment is never sent to a server). Older links of the form `/join#token=<key>` and a bare `#token=<key>` are read too, so a person
 * can paste whatever they were given.
 *
 * The key is kept only as long as the join takes: while the person signs in (sessionStorage, this tab only, for 30 minutes), and it
 * is removed as soon as it is used, cancelled, refused or expired. It is never written to the address bar after the page has read it
 * (see JoinPage) and never logged.
 */
export const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;
const FROM_LINK = /(?:#\/join\/|#token=|[?&]token=|\/join\/)([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])/;
const KEY = "threadforge.pendingInvitation";
const LIFETIME_MS = 30 * 60 * 1000;

/** The key inside whatever the person pasted (a bare key, or a link), or null when there is none. Bounded: long input is refused. */
export function parseInvitation(input: string): string | null {
  const text = input.trim();
  if (text.length === 0 || text.length > 2048) return null;
  if (TOKEN_RE.test(text)) return text;
  const found = FROM_LINK.exec(text);
  return found ? found[1]! : null;
}

interface Stored {
  token: string;
  at: number;
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export const pendingInvitation = {
  save(token: string, now = Date.now()): void {
    try {
      storage()?.setItem(KEY, JSON.stringify({ token, at: now } satisfies Stored));
    } catch {
      /* storage refused: the person will have to open the link again after signing in */
    }
  },
  read(now = Date.now()): string | null {
    try {
      const raw = storage()?.getItem(KEY);
      if (!raw) return null;
      const stored = JSON.parse(raw) as Partial<Stored>;
      if (typeof stored.token !== "string" || typeof stored.at !== "number" || now - stored.at > LIFETIME_MS || !TOKEN_RE.test(stored.token)) {
        this.clear();
        return null;
      }
      return stored.token;
    } catch {
      return null;
    }
  },
  clear(): void {
    try {
      storage()?.removeItem(KEY);
    } catch {
      /* nothing to clear */
    }
  },
};

/** Remove the key from the address bar without adding a history entry (the page has read it). */
export function scrubAddressBar(target = "#/join"): void {
  try {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${target}`);
  } catch {
    window.location.hash = target;
  }
}

/** The route that opens an invitation. */
export const joinHash = (token?: string) => (token ? `#/join/${token}` : "#/join");
