# ADR-104: GitHub Flavored Markdown and linked URLs in the web client

Status: Accepted (2026-11-28)

## Context
- Sessions write GitHub Flavored Markdown: bare URLs ("SEC-020: https://github.com/…/pull/6371") and tables. The web
  client rendered Markdown with `react-markdown` alone (decision WEB-05 8: "No GFM plugin (tables are plain text)").
  CommonMark knows neither autolink literals nor tables, so a bare URL was plain text and a table one paragraph of
  `|` and `---`.
- Prompts of persons are plain text (06-web.md §5.2); a URL in a prompt was not clickable either. The same holds for the
  plain text shown until the lazily loaded Markdown chunk arrives, the question of the situation view and the texts of
  operation cards.
- 06-web.md §2.1 allows no further runtime dependency without an ADR.

## Decision
- **`remark-gfm` 4.0.1** (MIT, pinned exactly, from the unified collective that also maintains `react-markdown`) as
  the one remark plugin of the Markdown renderer. It brings autolink literals (`https://…`, `www.…`, e-mail addresses),
  tables, strikethrough, task lists and footnotes. Everything else stays: `skipHtml`, no `rehype-raw`, every link and
  image source through `safeUrl` (`http:`, `https:`, `mailto:` only; any other scheme stays text), links with
  `rel="noopener noreferrer nofollow"` in a new tab, images as links. The renderer stays in its lazily loaded chunk, so
  the initial JavaScript does not grow.
- **Tables** stand in a scroll box of their own (`overflow-x: auto`, `tabindex="0"`), like code blocks (§12). The column
  alignment of GFM becomes a Tailwind class, never an inline style (CSP).
- **Plain texts** keep being plain text and never become Markdown: prompts, the fallback until the Markdown chunk is
  loaded (chat outputs and whiteboard entries), the question of the situation view and the subject of an operation card
  link their bare `http://`, `https://` and `mailto:` URLs (`linkParts` in `core/chat/links.ts`, rendered by
  `ui/task-tabs/chat/LinkedText.tsx`) under the same `safeUrl` rule, `LINK_REL` and new tab. Texts from GitHub in an
  operation card (issue text and comments) link `https:` URLs only, as 06-web.md §5.2 requires. Linked URLs in running
  text are inline targets (WCAG 2.5.8 exception, decision WEB-13).
- The trimming of closing punctuation is shared with the community's links (`trimUrl`).

## Consequences
- 18 packages more in the lockfile (the GFM parts of micromark and mdast, `remark-stringify`, `markdown-table`), all
  MIT, only in the Markdown chunk.
- A Markdown footnote produces in-page links (`#user-content-fn-…`); `safeUrl` drops relative URLs, so they stay text.

## Alternatives considered
- An own autolink pass over the Markdown tree (no dependency): solves the URLs but not the tables; a table parser of
  our own would duplicate `micromark-extension-gfm-table`.
- Rendering prompts as Markdown: changes how persons' prompts look (06-web.md §5.2 keeps them plain text).

## Deviation from DESIGN.md
None.
