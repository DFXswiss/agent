// Plain text with its bare URLs as links (06-web.md section 2.1): prompts, the plain text shown until the Markdown
// chunk is loaded, the question on the home screen and the texts of operation cards. The text stays React text (never
// HTML or Markdown); `linkParts` keeps only URLs that `safeUrl` keeps, of `schemes` (default `http:`, `https:`,
// `mailto:`; texts from GitHub pass `https:` only). Links open in a new tab with `LINK_REL`; they are inline targets
// inside running text (WCAG 2.5.8 exception, decision WEB-13).
import { Fragment } from 'react';
import { LINK_REL, linkParts } from '../../../core/chat/links';

export const HTTPS_ONLY: ReadonlySet<string> = new Set(['https:']);

export function LinkedText({ text, schemes }: { readonly text: string; readonly schemes?: Iterable<string> }) {
  return (
    <>
      {linkParts(text, schemes === undefined ? undefined : new Set(schemes)).map((part, index) =>
        part.kind === 'text' ? (
          <Fragment key={index}>{part.text}</Fragment>
        ) : (
          <a
            key={index}
            className="text-blue-800 underline"
            href={part.href}
            rel={LINK_REL}
            target="_blank"
            data-inline-link=""
          >
            {part.href}
          </a>
        ),
      )}
    </>
  );
}
