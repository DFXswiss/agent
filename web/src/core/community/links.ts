// Links in community texts (14-community.md section 6): `http://` and `https://` URLs become links, everything else
// stays text – never HTML. A URL ends at white space; trailing punctuation that usually closes a sentence (`.,;:!?`
// and closing brackets or quotes without an opening one inside the URL) stays text.
export type Segment =
  { readonly kind: 'text'; readonly text: string } | { readonly kind: 'link'; readonly href: string };

const URL_RE = /https?:\/\/[^\s<>"]+/gi;
const TRAILING = /[.,;:!?'"]$/;
const PAIRS: Readonly<Record<string, string>> = { ')': '(', ']': '[', '}': '{' };

const count = (text: string, char: string) => text.split(char).length - 1;

function trimUrl(url: string): string {
  let end = url;
  for (;;) {
    const last = end.slice(-1);
    const open = PAIRS[last];
    const unbalanced = open !== undefined && count(end, open) < count(end, last);
    if (!TRAILING.test(last) && !unbalanced) return end;
    end = end.slice(0, -1);
  }
}

/** Whether the browser parses the (already `http(s)://`) URL; a link to something unparsable stays text. */
function valid(href: string): boolean {
  try {
    new URL(href);
    return true;
  } catch {
    return false;
  }
}

/** The text split into plain text and link segments (adjacent text is merged). */
export function segments(text: string): Segment[] {
  const out: Segment[] = [];
  const push = (part: string) => {
    if (part === '') return;
    const last = out[out.length - 1];
    if (last?.kind === 'text') out[out.length - 1] = { kind: 'text', text: last.text + part };
    else out.push({ kind: 'text', text: part });
  };
  let at = 0;
  for (const match of text.matchAll(URL_RE)) {
    const href = trimUrl(match[0]);
    push(text.slice(at, match.index));
    if (valid(href)) out.push({ kind: 'link', href });
    else push(href);
    at = match.index + href.length;
  }
  push(text.slice(at));
  return out;
}
