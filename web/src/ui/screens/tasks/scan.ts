// The bounded title search and "only mine" of a task list (06-web.md section 5.1a, ADR-063 section 1). Titles are
// encrypted, so both filters run in the browser over the loaded rows; a rare match may sit on a page not loaded yet.
// One search request (a new search text or filter, or "Search further") loads at most `SCAN_PAGES` further pages –
// one `loadTasks` call each, always of the list that binds the merged order – and stops earlier once a page of matches
// is shown or no list has a further page. The view then says how many tasks were searched and offers to search
// further; it never loads every page on its own.
import { useEffect, useState } from 'react';
import { loadTasks, PAGE_SIZE } from '../../../core/tasks/list';
import { type Overview, overview, type OverviewQuery } from '../../../core/tasks/overview';
import type { TasksContext } from '../../../core/tasks/pull';

/** Further pages one search request loads at most. */
export const SCAN_PAGES = 10;

/** What the view shows of a bounded search. */
export interface SearchScan {
  /** Rows searched: the merged rows loaded so far. */
  readonly scanned: number;
  /** Tasks in the searched lists when the hub says it, else null. */
  readonly total: number | null;
  /** A list has a further page. */
  readonly hasMore: boolean;
  /** A search request is loading pages. */
  readonly loading: boolean;
  /** Starts the next search request (`SCAN_PAGES` further pages). */
  readonly searchFurther: () => void;
}

/** Resolves once no list of `query` is loading (a page requested by "Load more" or an earlier request). */
export function idle(context: TasksContext, query: OverviewQuery): Promise<void> {
  return new Promise((resolve) => {
    const check = (): boolean => !overview(context.store.getState(), query).busy;
    if (check()) return resolve();
    const unsubscribe = context.store.subscribe(() => {
      if (!check()) return;
      unsubscribe();
      resolve();
    });
  });
}

/**
 * Loads further pages of `query` until `hits` rows match, no list has a further page, a load brings nothing (a
 * failure), `stopped()` turns true, or `SCAN_PAGES` pages were loaded. A load already running is awaited first (it
 * may come from the request this one replaced: typing a search text replaces the request at every key).
 */
export async function scanPages(
  context: TasksContext,
  query: OverviewQuery,
  hits: number,
  stopped: () => boolean,
): Promise<void> {
  for (let page = 0; page < SCAN_PAGES; page += 1) {
    await idle(context, query);
    const view = overview(context.store.getState(), query);
    if (stopped() || view.binding === null || view.rows.length >= hits) return;
    const rows = await loadTasks(context, view.binding, query.state, true);
    if (rows.length === 0) return;
  }
}

interface Further {
  /** The query the request belongs to. */
  readonly key: string;
  /** Requests made with "Search further" for this query. */
  readonly count: number;
  /** Matches wanted by the latest request. */
  readonly hits: number;
}

/**
 * The bounded search of `query` while `active` (a search text or "only mine" is set): a new query starts a request
 * for one page of matches; "Search further" starts one for one page more than shown.
 */
export function useSearchScan(
  context: TasksContext,
  query: OverviewQuery,
  view: Overview,
  active: boolean,
): SearchScan {
  const key = JSON.stringify(query);
  const [further, setFurther] = useState<Further | null>(null);
  const [finished, setFinished] = useState<string | null>(null);
  const own = further?.key === key ? further : null;
  const hits = own?.hits ?? PAGE_SIZE;
  const count = own?.count ?? 0;
  const token = `${key}#${count}`;
  const running = active && view.status === 'ready';
  useEffect(() => {
    if (!running) return;
    let stopped = false;
    void scanPages(context, query, hits, () => stopped).then(() => {
      if (!stopped) setFinished(token);
    });
    return () => {
      stopped = true;
    };
  }, [context, query, hits, token, running]);
  const hasMore = view.binding !== null;
  return {
    scanned: view.merged.length,
    total: null,
    hasMore,
    loading: running && finished !== token && hasMore && view.rows.length < hits,
    searchFurther: () => setFurther({ key, count: count + 1, hits: view.rows.length + PAGE_SIZE }),
  };
}
