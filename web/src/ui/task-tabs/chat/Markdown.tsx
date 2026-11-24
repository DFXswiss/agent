// Markdown of chat outputs (06-web.md section 2.1): `react-markdown` without raw HTML (`skipHtml`, no `rehype-raw`),
// links only to `http(s):`/`mailto:` with `rel="noopener noreferrer nofollow"` (`core/chat/links.ts`); images are
// shown as links (the CSP allows no foreign images, and a remote image would reveal the reader). Loaded lazily
// (section 12.2).
import type { ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import { imageLabel, LINK_REL, safeUrl } from '../../../core/chat/links';

function Link({ href, children }: { readonly href: string; readonly children?: ReactNode }) {
  const url = safeUrl(href);
  if (url === null) return <span data-testid="markdown.link-removed">{children}</span>;
  return (
    <a href={url} rel={LINK_REL} target="_blank">
      {children}
    </a>
  );
}

const COMPONENTS: Components = {
  h1: ({ children }) => <h1 className="text-lg font-semibold">{children}</h1>,
  h2: ({ children }) => <h2 className="text-base font-semibold">{children}</h2>,
  h3: ({ children }) => <h3 className="font-semibold">{children}</h3>,
  ul: ({ children }) => <ul className="list-disc pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5">{children}</ol>,
  // a scroll box of its own on narrow screens, reachable with the keyboard (06-web.md section 12)
  pre: ({ children }) => (
    <pre className="max-w-full overflow-x-auto rounded bg-gray-100 p-2 text-sm" tabIndex={0}>
      {children}
    </pre>
  ),
  code: ({ children }) => <code className="font-mono text-[0.875em]">{children}</code>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-4 border-gray-300 pl-3 text-gray-600">{children}</blockquote>
  ),
  a: ({ href, children }) => <Link href={String(href)}>{children}</Link>,
  img: ({ src, alt }) => <Link href={String(src)}>{imageLabel(alt, src)}</Link>,
};

export default function Markdown({ text }: { readonly text: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-2 [overflow-wrap:anywhere]" data-testid="markdown">
      <ReactMarkdown skipHtml urlTransform={(url) => url} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
