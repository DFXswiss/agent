// Newest-first paging of the event lists a view shows (ADR-063 section 1, 06-web.md section 5.2): the chat of a task,
// the chat of a session and similar lists read the **newest** page of their query when they open and older pages only
// on demand ("Earlier", `loadEarlier`) – never every page in a loop. The paging state lives in the `pages` slice
// (`selectPages`).
//
// `openNewest` reads the newest page unless the list already holds it: once per store generation, and again when the
// event reader skipped bulk events of the task or session meanwhile (no view showed them, `focus.skipMark`) or after
// `resync` (`force`). After such a re-read the list's stored bulk events below the new page lie before a hole: they are
// dropped (the "Earlier" control reads them again), so a list never shows a silent gap. Concurrent calls for the same
// list share one request; answers that arrive after a sign-out write nothing.
import { eventsBefore, type EventsQuery, NEWEST, PAGE_DEFAULT } from '../net/pages';
import { HubError } from '../net/signedFetch';
import { skipMark } from '../store/focus';
import { classOf, dropEvents, type EventEntry, kindOf } from '../store/slices/events';
import { NO_PAGE, type PageState, setPage } from '../store/slices/pages';
import type { Selector, Store } from '../store/store';
import type { TasksContext } from './pull';
import { isRecord } from '../registry';

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

/** The paging state of a list (`NO_PAGE` before its first page). */
export const selectPages =
  (list: PagedList): Selector<PageState> =>
  (state) =>
    state.pages[pagesKey(list)] ?? NO_PAGE;

const codeOf = (error: unknown): string => (error instanceof HubError ? error.code : 'unavailable');

const running = new WeakMap<Store, Map<string, Promise<void>>>();

/** Runs `work` once per key at a time (concurrent callers share it). */
function shared(store: Store, key: string, work: () => Promise<void>): Promise<void> {
  const map = running.get(store) ?? new Map<string, Promise<void>>();
  running.set(store, map);
  const known = map.get(key);
  if (known !== undefined) return known;
  const run = work().finally(() => map.delete(key));
  map.set(key, run);
  return run;
}

/** Whether a stored entry belongs to the list's query (by its unchecked header: unverifiable rows too). */
function matches(entry: EventEntry, query: EventsQuery): boolean {
  const header = isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};
  return (
    (query.kinds === undefined || query.kinds.includes(kindOf(entry) as string)) &&
    (query.task === undefined || header.task === query.task) &&
    (query.session === undefined || header.session === query.session) &&
    (query.ref === undefined || header.ref === query.ref)
  );
}

/** Reads the newest page of the list unless it holds it already (module comment). */
export function openNewest(context: TasksContext, list: PagedList, force: boolean = false): Promise<void> {
  const { store } = context;
  const key = pagesKey(list);
  const mark = skipMark(store, list.query);
  const known = store.getState().pages[key];
  if (!force && known !== undefined && known.status !== 'error' && known.mark === mark) return Promise.resolve();
  return shared(store, `newest ${key}`, async () => {
    const generation = store.generation;
    if (known === undefined || known.status === 'error') setPage(store, key, { status: 'loading', code: null });
    try {
      const page = await eventsBefore(context.client, list.scope, list.query, NEWEST, list.limit ?? PAGE_DEFAULT);
      if (store.generation !== generation) return;
      await context.ingest(list.org, list.scope, page.events);
      const oldest = page.events[0]?.scope_seq;
      if (known !== undefined && oldest !== undefined && page.nextBefore !== null) {
        dropEvents(
          store,
          list.scope,
          (entry) => entry.scopeSeq < oldest && classOf(entry) === 'bulk' && matches(entry, list.query),
        );
      }
      setPage(store, key, { status: 'ready', code: null, before: page.nextBefore, earlier: false, mark });
    } catch (error) {
      if (store.generation === generation) setPage(store, key, { status: 'error', code: codeOf(error) });
    }
  });
}

/** Reads the next older page of the list (no effect before its newest page or when nothing older exists). */
export function loadEarlier(context: TasksContext, list: PagedList): Promise<void> {
  const { store } = context;
  const key = pagesKey(list);
  return shared(store, `earlier ${key}`, async () => {
    const known = store.getState().pages[key];
    if (known === undefined || known.status !== 'ready' || known.before === null) return;
    const generation = store.generation;
    setPage(store, key, { earlier: true, code: null });
    try {
      const page = await eventsBefore(context.client, list.scope, list.query, known.before, list.limit ?? PAGE_DEFAULT);
      if (store.generation !== generation) return;
      await context.ingest(list.org, list.scope, page.events);
      setPage(store, key, { before: page.nextBefore, earlier: false });
    } catch (error) {
      if (store.generation === generation) setPage(store, key, { earlier: false, code: codeOf(error) });
    }
  });
}
