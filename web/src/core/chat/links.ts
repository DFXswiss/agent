// Link rules of rendered Markdown (06-web.md section 2.1): only `http:`, `https:` and `mailto:` URLs are kept; every
// other scheme (`javascript:`, `data:`, `vbscript:`, `file:`, …), relative and protocol-relative URLs and anything
// that does not parse is dropped. Links open with `rel="noopener noreferrer nofollow"`. Plain texts (prompts, texts of
// operation cards) get the same links for bare URLs through `linkParts`.
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

/** A part of a plain text: text, or a URL that may be rendered as a link. */
export type TextPart =
  { readonly kind: 'text'; readonly text: string } | { readonly kind: 'link'; readonly href: string };

const TEXT_URL = /(?:https?:\/\/|mailto:)[^\s<>"]+/gi;
const SCHEME = /^(?:https?:\/\/|mailto:)/i;
const TRAILING = /[.,;:!?'"]$/;
const PAIRS: Readonly<Record<string, string>> = { ')': '(', ']': '[', '}': '{' };

const count = (text: string, char: string) => text.split(char).length - 1;

/** The URL without the punctuation that usually closes a sentence (`.,;:!?`, quotes, unbalanced closing brackets). */
export function trimUrl(url: string): string {
  let end = url;
  for (;;) {
    const last = end.slice(-1);
    const open = PAIRS[last];
    const unbalanced = open !== undefined && count(end, open) < count(end, last);
    if (!TRAILING.test(last) && !unbalanced) return end;
    end = end.slice(0, -1);
  }
}

/**
 * The plain text split into text and link parts (adjacent text merged): `http://`, `https://` and `mailto:` URLs become
 * links when `safeUrl` keeps them and their scheme is one of `schemes`; a URL ends at white space, `<`, `>` or `"`.
 */
export function linkParts(text: string, schemes: ReadonlySet<string> = ALLOWED): TextPart[] {
  const out: TextPart[] = [];
  const push = (part: string) => {
    if (part === '') return;
    const last = out[out.length - 1];
    if (last?.kind === 'text') out[out.length - 1] = { kind: 'text', text: last.text + part };
    else out.push({ kind: 'text', text: part });
  };
  let at = 0;
  for (const match of text.matchAll(TEXT_URL)) {
    const href = trimUrl(match[0]);
    const url = href.replace(SCHEME, '') === '' ? null : safeUrl(href);
    push(text.slice(at, match.index));
    if (url !== null && schemes.has(new URL(url).protocol)) out.push({ kind: 'link', href: url });
    else push(href);
    at = match.index + href.length;
  }
  push(text.slice(at));
  return out;
}

/** The text of an image shown as a link: its alternative text, else its source. */
export function imageLabel(alt: string | undefined, src: unknown): string {
  return alt === undefined || alt === '' ? String(src) : alt;
}
