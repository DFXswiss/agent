// Slice `pages` (ADR-086 section 1, 06-web.md section 2.3): the newest-first paging state of the event lists a view
// shows (chat, session chat; the title pages of a workgroup), by a key of scope and query (`core/tasks/pages.ts`):
//
// - `status`: the newest page is `loading`, `ready` or failed (`error` with the code);
// - `before`: `before` of the next older page, null when everything older is loaded (`hasEarlier`). It is also the
//   list's **boundary**: the list holds every matching event at or above it, so a view shows only those (below it a
//   stored event may lie behind a hole). When the event store evicts or drops a matching event at or above it, the
//   boundary rises above that event (`slices/events.ts`), so "Earlier" reads it again – never a silent gap;
// - `earlier`: an older page is being loaded (the "Earlier" control shows it);
// - `mark`: what the reader had skipped of the task/session when the newest page was read (`focus.skipMark`);
// - `list`: the scope and query of the list (set by its first read; the event store matches evicted events by it,
//   and keeps what a mounted list holds at or above its boundary, `slices/events.ts`).
import type { Slice, Store } from '../store';

export type PageStatus = 'loading' | 'ready' | 'error';

/** The query of a paged list (the shape of `net/pages.ts` `EventsQuery`). */
export interface ListQuery {
  readonly kinds?: readonly string[];
  readonly task?: string;
  readonly session?: string;
  readonly ref?: string;
}

export interface PageState {
  readonly status: PageStatus;
  readonly code: string | null;
  readonly before: number | null;
  readonly earlier: boolean;
  readonly mark: string | null;
  readonly list: { readonly scope: string; readonly query: ListQuery } | null;
}

export type PagesState = Readonly<Record<string, PageState>>;

declare module '../store' {
  interface StoreState {
    pages: PagesState;
  }
}

export const slice: Slice<'pages'> = { name: 'pages', initial: () => ({}) };

export const NO_PAGE: PageState = {
  status: 'loading',
  code: null,
  before: null,
  earlier: false,
  mark: null,
  list: null,
};

/** Changes the paging state of `key` (from `NO_PAGE` if it has none); no update when nothing changed. */
export function setPage(store: Store, key: string, patch: Partial<PageState>): void {
  store.update('pages', (pages) => {
    const known = pages[key] ?? NO_PAGE;
    const next = { ...known, ...patch };
    const same =
      pages[key] !== undefined && (Object.keys(next) as (keyof PageState)[]).every((k) => next[k] === known[k]);
    return same ? pages : { ...pages, [key]: next };
  });
}

/** The boundary of a list: the lowest scope seq a view of it shows (0: everything, module comment). */
export const boundaryOf = (page: PageState | undefined): number => page?.before ?? 0;
