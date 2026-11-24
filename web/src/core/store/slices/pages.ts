// Slice `pages` (ADR-063 section 1, 06-web.md section 2.3): the newest-first paging state of the event lists a view
// shows (chat, session chat, operations of a task), by a key of scope and query (`core/tasks/pages.ts`):
//
// - `status`: the newest page is `loading`, `ready` or failed (`error` with the code);
// - `before`: `before` of the next older page, null when everything older is loaded (`hasEarlier`);
// - `earlier`: an older page is being loaded (the "Earlier" control shows it);
// - `mark`: what the reader had skipped of the task/session when the newest page was read (`focus.skipMark`).
import type { Slice, Store } from '../store';

export type PageStatus = 'loading' | 'ready' | 'error';

export interface PageState {
  readonly status: PageStatus;
  readonly code: string | null;
  readonly before: number | null;
  readonly earlier: boolean;
  readonly mark: string | null;
}

export type PagesState = Readonly<Record<string, PageState>>;

declare module '../store' {
  interface StoreState {
    pages: PagesState;
  }
}

export const slice: Slice<'pages'> = { name: 'pages', initial: () => ({}) };

export const NO_PAGE: PageState = { status: 'loading', code: null, before: null, earlier: false, mark: null };

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
