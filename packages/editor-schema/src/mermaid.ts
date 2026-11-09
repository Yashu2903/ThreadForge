/**
 * Mermaid diagram rules shared by every place a diagram is checked or drawn: the browser (SafeMermaid),
 * the Mermaid parse check (packages/mermaid-check) and, as a Python mirror, the AI worker
 * (backend/app/ai/answer/mermaid.py). Both implementations are tested against the SAME cases
 * (test/fixtures/mermaid-cases.json), so they cannot drift apart.
 *
 * These are STATIC rules: pure text checks that need no Mermaid. A diagram must be one of the allowed
 * types, at most 20,000 characters and 500 lines, and may contain no directives (`%%{init}%%` can change
 * Mermaid's security level), no `click`/`href`/`link` interaction, no HTML tags, no URLs, no
 * `javascript:`/`data:` schemes and no HTML entities. Whether the text is valid Mermaid syntax is a
 * separate question, answered by the real parser.
 */

export const MERMAID_MAX_CHARS = 20_000;
export const MERMAID_MAX_LINES = 500;
export const MERMAID_TYPES = [
  "flowchart",
  "graph",
  "erDiagram",
  "sequenceDiagram",
  "classDiagram",
  "stateDiagram-v2",
  "stateDiagram",
  "gantt",
  "mindmap",
  "timeline",
] as const;

export type MermaidCheck = { ok: true } | { ok: false; code: string; message: string };

const FIRST = new RegExp(`^(${MERMAID_TYPES.map((t) => t.replace(/[.*+?^${}()|[\]\\-]/g, "\\$&")).join("|")})(?![\\w-])`);
const FORBIDDEN_LINE = /^\s*(click|href|link|callback|linkStyle\s+\S+\s+href)\b/i;
const FORBIDDEN: ReadonlyArray<readonly [string, RegExp]> = [
  ["html_not_allowed", /<\s*\/?\s*[A-Za-z!]/],
  ["url_not_allowed", /[A-Za-z][A-Za-z0-9+.-]*:\/\/|\bwww\./i],
  ["scheme_not_allowed", /\b(javascript|data|vbscript)\s*:/i],
  ["entity_not_allowed", /&#?[A-Za-z0-9]+;/],
  ["interaction_not_allowed", /\bhref\b|\bonclick\b|\bon[a-z]+\s*=/i],
];
// eslint-disable-next-line no-control-regex
const CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/;

const fail = (code: string, message: string): MermaidCheck => ({ ok: false, code, message });

export function validateMermaidSource(source: string): MermaidCheck {
  if (typeof source !== "string" || !source.trim()) return fail("empty", "The diagram is empty.");
  if (source.length > MERMAID_MAX_CHARS) return fail("too_long", `A diagram is limited to ${MERMAID_MAX_CHARS} characters.`);
  if (source.split("\n").length > MERMAID_MAX_LINES) {
    return fail("too_many_lines", `A diagram is limited to ${MERMAID_MAX_LINES} lines.`);
  }
  if (CONTROL.test(source)) return fail("control_characters", "The diagram contains control characters.");
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const body = lines.filter((ln) => ln.trim() && !ln.trimStart().startsWith("%%"));
  if (lines.some((ln) => ln.includes("%%{"))) {
    return fail("directive_not_allowed", "Mermaid directives (%%{ ... }%%) are not allowed.");
  }
  if (body.length === 0) return fail("empty", "The diagram is empty.");
  if (!FIRST.test(body[0]!.trim())) {
    return fail("type_not_allowed", `The diagram must start with one of: ${MERMAID_TYPES.join(", ")}.`);
  }
  for (const ln of body.slice(1)) {
    if (FORBIDDEN_LINE.test(ln)) return fail("interaction_not_allowed", "click, href and link are not allowed.");
  }
  for (const [code, pattern] of FORBIDDEN) {
    if (pattern.test(source)) return fail(code, "The diagram contains something that is not allowed.");
  }
  return { ok: true };
}
