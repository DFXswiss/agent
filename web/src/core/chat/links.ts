// Link rules of rendered Markdown (06-web.md section 2.1): only `http:`, `https:` and `mailto:` URLs are kept; every
// other scheme (`javascript:`, `data:`, `vbscript:`, `file:`, …), relative and protocol-relative URLs and anything
// that does not parse is dropped. Links open with `rel="noopener noreferrer nofollow"`.
export const LINK_REL = 'noopener noreferrer nofollow';
const ALLOWED = new Set(['http:', 'https:', 'mailto:']);

const isControl = (char: string): boolean => char.charCodeAt(0) < 0x20 || char.charCodeAt(0) === 0x7f;

/** The URL if it may be rendered as a link or image source, else null. */
export function safeUrl(url: string): string | null {
  const trimmed = url.trim();
  // only absolute URLs with an explicit scheme; control characters (e.g. "java\tscript:") are refused outright
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(trimmed) || [...trimmed].some(isControl)) return null;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  return ALLOWED.has(parsed.protocol) ? trimmed : null;
}

/** The text of an image shown as a link: its alternative text, else its source. */
export function imageLabel(alt: string | undefined, src: unknown): string {
  return alt === undefined || alt === '' ? String(src) : alt;
}
