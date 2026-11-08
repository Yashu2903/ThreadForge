/** Deterministic JSON text: object keys sorted, undefined dropped, no whitespace. */
export function canonicalStringify(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalStringify).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const o = value as Record<string, unknown>;
    return (
      "{" +
      Object.keys(o)
        .filter((k) => o[k] !== undefined)
        .sort()
        .map((k) => JSON.stringify(k) + ":" + canonicalStringify(o[k]))
        .join(",") +
      "}"
    );
  }
  return JSON.stringify(value) ?? "null";
}
