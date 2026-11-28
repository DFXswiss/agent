// Markdown of chat outputs and whiteboard entries (06-web.md section 2.1): `react-markdown` with GitHub Flavored
// Markdown (`remark-gfm`, ADR-104: bare URLs, tables, strikethrough, task lists, footnotes) and without raw HTML
// (`skipHtml`, no `rehype-raw`), links only to `http(s):`/`mailto:` with `rel="noopener noreferrer nofollow"` in a new
// tab (`core/chat/links.ts`); images are shown as links (the CSP allows no foreign images, and a remote image would
// reveal the reader). Code blocks and tables scroll in a box of their own that the keyboard reaches (section 12).
// Loaded lazily (section 12.2).
import type { CSSProperties, ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
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

// a column's alignment (`:--`, `:-:`, `--:`) as a class: no inline style (CSP of 03-api.md section 1)
const ALIGN: Readonly<Record<string, string>> = { left: 'text-left', center: 'text-center', right: 'text-right' };
const cell = (style: CSSProperties | undefined) =>
  `border border-gray-300 px-2 py-1 align-top ${ALIGN[String(style?.textAlign)] ?? 'text-left'}`;

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
  table: ({ children }) => (
    <div className="max-w-full overflow-x-auto" tabIndex={0} data-testid="markdown.table">
      <table className="border-collapse text-sm [overflow-wrap:normal]">{children}</table>
    </div>
  ),
  th: ({ children, style }) => <th className={`${cell(style)} bg-gray-100 font-semibold`}>{children}</th>,
  td: ({ children, style }) => <td className={cell(style)}>{children}</td>,
  code: ({ children }) => <code className="font-mono text-[0.875em]">{children}</code>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-4 border-gray-300 pl-3 text-gray-600">{children}</blockquote>
  ),
  a: ({ href, children }) => <Link href={String(href)}>{children}</Link>,
  img: ({ src, alt }) => <Link href={String(src)}>{imageLabel(alt, src)}</Link>,
};

const PLUGINS = [remarkGfm];

export default function Markdown({ text }: { readonly text: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-2 [overflow-wrap:anywhere]" data-testid="markdown">
      <ReactMarkdown skipHtml remarkPlugins={PLUGINS} urlTransform={(url) => url} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
