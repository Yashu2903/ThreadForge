/**
 * Where the caret may stop: grapheme-cluster and word boundaries, as UTF-16 offsets into a string.
 *
 * Intl.Segmenter does the real work (emoji ZWJ sequences, skin tones, flags, combining marks,
 * Hangul, Indic scripts). It is available in current Chromium, Safari 14.1+ and Firefox 125+. For a
 * browser without it there is a documented FALLBACK that is correct for surrogate pairs, combining
 * marks (\p{M}), variation selectors, zero-width joiner sequences and regional-indicator pairs but
 * not for scripts that need a dictionary or complex-cluster rules (Thai word breaks, Indic conjuncts).
 */

type SegmenterCtor = new (
  locale?: string,
  options?: { granularity: "grapheme" | "word" },
) => { segment(input: string): Iterable<{ segment: string; index: number; isWordLike?: boolean }> };

const Segmenter = (Intl as unknown as { Segmenter?: SegmenterCtor }).Segmenter;
let forceFallback = false;

/** Test hook: pretend Intl.Segmenter does not exist. */
export function useFallbackSegmentation(on: boolean): void {
  forceFallback = on;
}
export const hasSegmenter = (): boolean => !!Segmenter && !forceFallback;

let graphemeSeg: InstanceType<SegmenterCtor> | null = null;
let wordSeg: InstanceType<SegmenterCtor> | null = null;

export interface Segment {
  start: number;
  end: number;
  wordLike: boolean;
}

const COMBINING = /^[\p{M}‍︎️\u{1f3fb}-\u{1f3ff}\u{e0020}-\u{e007f}]$/u;
const REGIONAL = /^[\u{1f1e6}-\u{1f1ff}]$/u;

function fallbackGraphemes(text: string): Segment[] {
  const out: Segment[] = [];
  const cps = Array.from(text);
  let offset = 0;
  let i = 0;
  while (i < cps.length) {
    let j = i + 1;
    let end = offset + cps[i]!.length;
    if (REGIONAL.test(cps[i]!) && j < cps.length && REGIONAL.test(cps[j]!)) {
      end += cps[j]!.length;
      j++;
    }
    while (j < cps.length) {
      const next = cps[j]!;
      if (COMBINING.test(next)) {
        end += next.length;
        j++;
        if (next === "‍" && j < cps.length) {
          // ZWJ joins the following code point into the same cluster
          end += cps[j]!.length;
          j++;
        }
      } else break;
    }
    out.push({ start: offset, end, wordLike: true });
    offset = end;
    i = j;
  }
  return out;
}

function fallbackWords(text: string): Segment[] {
  const out: Segment[] = [];
  const re = /[\p{L}\p{N}_'’]+|\s+|[^\p{L}\p{N}_\s]/gu;
  for (const m of text.matchAll(re)) {
    out.push({ start: m.index!, end: m.index! + m[0].length, wordLike: /[\p{L}\p{N}]/u.test(m[0]) });
  }
  return out;
}

export function graphemes(text: string): Segment[] {
  if (!hasSegmenter()) return fallbackGraphemes(text);
  graphemeSeg ??= new Segmenter!(undefined, { granularity: "grapheme" });
  return Array.from(graphemeSeg.segment(text), (s) => ({ start: s.index, end: s.index + s.segment.length, wordLike: true }));
}

export function words(text: string): Segment[] {
  if (!hasSegmenter()) return fallbackWords(text);
  wordSeg ??= new Segmenter!(undefined, { granularity: "word" });
  return Array.from(wordSeg.segment(text), (s) => ({
    start: s.index,
    end: s.index + s.segment.length,
    wordLike: !!s.isWordLike,
  }));
}

/** The start of the grapheme that ends at `offset` (offset itself if it is 0). */
export function prevGrapheme(text: string, offset: number): number {
  if (offset <= 0) return 0;
  let best = 0;
  for (const g of graphemes(text)) {
    if (g.start < offset) best = g.start;
    else break;
  }
  return best;
}

/** The end of the grapheme that starts at (or contains) `offset`. */
export function nextGrapheme(text: string, offset: number): number {
  if (offset >= text.length) return text.length;
  for (const g of graphemes(text)) {
    if (g.end > offset) return g.end;
  }
  return text.length;
}

/** Ctrl+Backspace: skip whitespace before the caret, then delete one word (or one run of punctuation). */
export function prevWordBoundary(text: string, offset: number): number {
  const segs = words(text).filter((s) => s.start < offset);
  let i = segs.length - 1;
  while (i >= 0 && /^\s+$/.test(text.slice(segs[i]!.start, segs[i]!.end))) i--;
  return i < 0 ? 0 : segs[i]!.start;
}

/** Ctrl+Delete: one word (or run of punctuation) after the caret, then the whitespace that follows it. */
export function nextWordBoundary(text: string, offset: number): number {
  const segs = words(text).filter((s) => s.end > offset);
  let i = 0;
  if (segs[i] && /^\s+$/.test(text.slice(segs[i]!.start, segs[i]!.end))) i++;
  if (i < segs.length) i++;
  while (i < segs.length && /^\s+$/.test(text.slice(segs[i]!.start, segs[i]!.end))) i++;
  return i === 0 || segs.length === 0 ? text.length : (segs[i - 1]?.end ?? text.length);
}
