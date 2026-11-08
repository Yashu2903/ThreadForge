/**
 * Structured JSON logging. The rule is enforced by construction: callers pass SHORT, NAMED, scalar
 * fields (document id, counts, codes). There is no way to hand this a document, a cookie, a header or
 * a token, and the filter below drops any field whose NAME suggests one, as a second line of defence.
 */
export type Level = "debug" | "info" | "warn" | "error" | "silent";
const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40, silent: 100 };
const FORBIDDEN = /cookie|secret|token|authorization|content|text|body|state/i;

export type Fields = Record<string, string | number | boolean | null | undefined>;

export interface Logger {
  debug(msg: string, fields?: Fields): void;
  info(msg: string, fields?: Fields): void;
  warn(msg: string, fields?: Fields): void;
  error(msg: string, fields?: Fields): void;
}

export function createLogger(
  level: Level = "info",
  sink: (line: string) => void = (line) => process.stdout.write(line + "\n"),
): Logger {
  const emit = (lvl: Exclude<Level, "silent">, msg: string, fields?: Fields) => {
    if (ORDER[lvl] < ORDER[level]) return;
    const safe: Fields = {};
    for (const [key, value] of Object.entries(fields ?? {})) {
      if (FORBIDDEN.test(key)) continue;
      safe[key] = typeof value === "string" && value.length > 200 ? value.slice(0, 200) : value;
    }
    sink(JSON.stringify({ time: new Date().toISOString(), level: lvl, msg, ...safe }));
  };
  return {
    debug: (m, f) => emit("debug", m, f),
    info: (m, f) => emit("info", m, f),
    warn: (m, f) => emit("warn", m, f),
    error: (m, f) => emit("error", m, f),
  };
}

export const silentLogger: Logger = createLogger("silent");
