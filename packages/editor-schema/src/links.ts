/**
 * The link policy (identical to docs/document-schema.md and the old editor's checkLink): only http,
 * https and mailto, case-insensitive; no whitespace/control/invisible characters anywhere (browsers
 * strip tabs and newlines inside URLs, which is how "java\nscript:" slips past naive filters); http(s)
 * needs "//host" with no credentials and no backslash; at most maxLength code points.
 * Returns the canonical href (lowercase scheme) or null.
 */

const SCHEME = /^([A-Za-z][A-Za-z0-9+.-]*):/;

export function isBadUrlChar(code: number): boolean {
  return (
    code <= 0x20 ||
    (code >= 0x7f && code <= 0xa0) ||
    code === 0x180e ||
    code === 0x1680 ||
    code === 0x3000 ||
    code === 0xfeff ||
    (code >= 0x2000 && code <= 0x200f) ||
    (code >= 0x2028 && code <= 0x202f) ||
    (code >= 0x205f && code <= 0x206f)
  );
}

export function checkLink(href: string, maxLength = 2048): string | null {
  const chars = Array.from(href);
  let start = 0;
  let end = chars.length;
  while (start < end && isBadUrlChar(chars[start]!.codePointAt(0)!)) start++;
  while (end > start && isBadUrlChar(chars[end - 1]!.codePointAt(0)!)) end--;
  const kept = chars.slice(start, end);
  if (kept.length === 0 || kept.length > maxLength) return null;
  if (kept.some((c) => isBadUrlChar(c.codePointAt(0)!))) return null;
  const s = kept.join("");
  const match = SCHEME.exec(s);
  if (!match) return null;
  const scheme = match[1]!.toLowerCase();
  if (scheme !== "http" && scheme !== "https" && scheme !== "mailto") return null;
  const rest = s.slice(match[0].length);
  if (scheme === "mailto") {
    if (!rest) return null;
  } else {
    if (!rest.startsWith("//") || s.includes("\\")) return null;
    const authority = rest.slice(2).split(/[/?#]/, 1)[0]!;
    if (!authority || authority.includes("@")) return null;
  }
  return scheme + ":" + rest;
}
