// Text of heavy list content (ADR-086 section 1): JSON pretty-printed before it is clipped, the one-line placeholder
// of content that is not near the viewport, and the clip of a large single entry.

/** Characters of a large text shown at a time ("Show more" adds as many). */
export const CLIP_CHARS = 20_000;

/** Characters of the one-line placeholder (the line is clipped by CSS as well). */
export const LINE_CHARS = 200;

/** `value` as indented JSON; a value JSON cannot express (`undefined`, a function) is the empty text. */
export function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? '';
}

/** The start of `text` on one line: line breaks and runs of white space become one space, at most `LINE_CHARS`. */
export function oneLine(text: string): string {
  const line = text
    .slice(0, LINE_CHARS * 4)
    .replace(/\s+/g, ' ')
    .trim();
  return line.length > LINE_CHARS ? `${line.slice(0, LINE_CHARS)}…` : line;
}

/** `text` cut to `limit` characters. */
export function clip(text: string, limit: number): string {
  return text.length > limit ? text.slice(0, limit) : text;
}
