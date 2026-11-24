// A paged event list and its key in the `pages` slice (`pages.ts`). A module of its own so that the task list of the
// first load (`list.ts`, the titles) does not pull the paging of the lazily loaded views.
import type { EventsQuery } from '../net/pages';

export interface PagedList {
  readonly org: string;
  readonly scope: string;
  readonly query: EventsQuery;
  /** Events per page (default 200). */
  readonly limit?: number;
}

/** The key of a list in the `pages` slice. */
export const pagesKey = (list: PagedList): string =>
  [
    list.scope,
    (list.query.kinds ?? []).join(','),
    list.query.task ?? '',
    list.query.session ?? '',
    list.query.ref ?? '',
  ].join(' ');
