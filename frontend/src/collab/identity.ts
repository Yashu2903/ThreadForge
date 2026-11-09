/**
 * Who is who in the document. Each collaborator announces `{ id }` through the awareness channel,
 * and WHAT THE SCREEN SHOWS comes from OUR member list, never from text a client sent: another
 * browser could announce any name it likes. Unknown ids (a member who just joined, or a made-up id)
 * are drawn as "Someone". Colours are derived from the id, so they cannot be spoofed either.
 */
export interface MemberName {
  user_id: string;
  display_name: string;
}

const PALETTE = ["#b3261e", "#1b6ec2", "#2e7d32", "#8e24aa", "#c75b00", "#00796b", "#ad1457", "#5d4037"];

export function colorFor(id: string): string {
  let hash = 0;
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length]!;
}

export function displayNameFor(id: unknown, members: readonly MemberName[]): string {
  if (typeof id !== "string") return "Someone";
  return members.find((m) => m.user_id === id)?.display_name ?? "Someone";
}

/** The element drawn at a collaborator's caret (CollaborationCaret `render`). Text is set with
 *  textContent, never innerHTML. */
export function renderCaret(user: { id?: unknown }, members: readonly MemberName[]): HTMLElement {
  const id = typeof user.id === "string" ? user.id : "";
  const color = colorFor(id);
  const caret = document.createElement("span");
  caret.classList.add("collab-caret");
  caret.style.borderColor = color;
  caret.setAttribute("data-user-id", id);
  // a name tag floating in the middle of someone's sentence must not be read out as part of it
  caret.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.classList.add("collab-caret-label");
  label.style.backgroundColor = color;
  label.textContent = displayNameFor(id, members);
  caret.appendChild(label);
  return caret;
}
