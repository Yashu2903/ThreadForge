/** Colours are stored as #RRGGBB (uppercase). Pasted CSS colours are converted or dropped, never kept as strings. */

const HEX6 = /^#[0-9a-fA-F]{6}$/;
const HEX3 = /^#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])$/;
const RGB = /^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*[\d.]+%?\s*)?\)$/i;

export const isHexColor = (value: unknown): value is string => typeof value === "string" && HEX6.test(value);

/** "#abc", "#aabbcc", "rgb(1, 2, 3)" to "#AABBCC"; anything else (named colours, var(), hsl()) to null. */
export function normalizeColor(css: string | null | undefined): string | null {
  if (!css) return null;
  const value = css.trim();
  if (HEX6.test(value)) return value.toUpperCase();
  const short = HEX3.exec(value);
  if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toUpperCase();
  const rgb = RGB.exec(value);
  if (rgb) {
    const parts = [rgb[1], rgb[2], rgb[3]].map(Number);
    if (parts.every((n) => n >= 0 && n <= 255)) {
      return "#" + parts.map((n) => n.toString(16).padStart(2, "0")).join("").toUpperCase();
    }
  }
  return null;
}
