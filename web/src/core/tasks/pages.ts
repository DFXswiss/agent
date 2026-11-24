// Newest-first paging of the event lists a view shows (ADR-086 section 1, 06-web.md section 5.2): the chat of a task,
// the chat of a session and similar lists read the **newest** page of their query when they open and older pages only
// on demand ("Earlier", `loadEarlier`) – never every page in a loop. The paging state lives in the `pages` slice
// (`selectPages`); its `before` is the list's boundary – a view shows only the events at or above it (`boundaryOf`).
//
// `openNewest` reads the newest page unless the list already holds it: once per store generation, and again when the
// event reader skipped bulk events of the task or session meanwhile (no view showed them, `focus.skipMark`) or after
// `resync` (`force`). Such a re-read moves the boundary up to the new page, so stored events below it (they may lie
// before a hole) are no longer shown; the stored bulk events of the list below it are dropped unless another mounted
// list still shows them (at or above its boundary), and every other list of the scope that held dropped events raises
// its boundary as well (`slices/events.ts`) – the "Earlier" control reads them again, a list never shows a silent gap. Concurrent calls for the same list share one request; a forced call while a read
// runs queues one more read after it (that read may have been answered before the lost frames); answers that arrive
// after a sign-out write nothing.
import { eventsBefore, NEWEST, PAGE_DEFAULT } from '../net/pages';
import { HubError } from '../net/signedFetch';
import { skipMark } from '../store/focus';
import { classOf, dropEvents, matchesQuery } from '../store/slices/events';
import { NO_PAGE, type PageState, setPage } from '../store/slices/pages';
import type { Selector, Store } from '../store/store';
import { type PagedList, pagesKey } from './paged-list';
import type { TasksContext } from './pull';

export { type PagedList, pagesKey } from './paged-list';

/** The paging state of a list (`NO_PAGE` before its first page). */
export const selectPages =
  (list: PagedList): Selector<PageState> =>
  (state) =>
    state.pages[pagesKey(list)] ?? NO_PAGE;

const codeOf = (error: unknown): string => (error instanceof HubError ? error.code : 'unavailable');

const running = new WeakMap<Store, Map<string, Promise<void>>>();
const queued = new WeakMap<Store, Map<string, Promise<void>>>();

const mapOf = (maps: WeakMap<Store, Map<string, Promise<void>>>, store: Store): Map<string, Promise<void>> => {
  const map = maps.get(store) ?? new Map<string, Promise<void>>();
  maps.set(store, map);
  return map;
};

/** Runs `work` once per key at a time (concurrent callers share it). */
function shared(store: Store, key: string, work: () => Promise<void>): Promise<void> {
  const map = mapOf(running, store);
  const known = map.get(key);
  if (known !== undefined) return known;
  const run = work().finally(() => map.delete(key));
  map.set(key, run);
  return run;
}

/** `shared`, but a forced call while `work` runs gets one more run after it (forced callers meanwhile share it). */
function fresh(store: Store, key: string, work: () => Promise<void>): Promise<void> {
  const known = mapOf(running, store).get(key);
  if (known === undefined) return shared(store, key, work);
  const waiting = mapOf(queued, store);
  const next =
    waiting.get(key) ??
    known.then(() => {
      waiting.delete(key);
      return shared(store, key, work);
    });
  waiting.set(key, next);
  return next;
}

/** Reads the newest page of the list unless it holds it already (module comment). */
export function openNewest(context: TasksContext, list: PagedList, force: boolean = false): Promise<void> {
  const { store } = context;
  const key = pagesKey(list);
  const known = store.getState().pages[key];
  if (!force && known !== undefined && known.status !== 'error' && known.mark === skipMark(store, list.query)) {
    return Promise.resolve();
  }
  return (force ? fresh : shared)(store, `newest ${key}`, async () => {
    const generation = store.generation;
    const mark = skipMark(store, list.query);
    const current = store.getState().pages[key];
    setPage(store, key, {
      ...((current === undefined || current.status === 'error') && { status: 'loading', code: null }),
      list: current?.list ?? { scope: list.scope, query: list.query },
    });
    try {
      const page = await eventsBefore(context.client, list.scope, list.query, NEWEST, list.limit ?? PAGE_DEFAULT);
      if (store.generation !== generation) return;
      // the boundary moves to the new page before its events arrive: nothing below it is shown any more
      setPage(store, key, { before: page.nextBefore });
      await context.ingest(list.org, list.scope, page.events);
      const oldest = page.events[0]?.scope_seq;
      if (oldest !== undefined && page.nextBefore !== null) {
        dropEvents(
          store,
          list.scope,
          (entry) => entry.scopeSeq < oldest && classOf(entry) === 'bulk' && matchesQuery(entry, list.query),
        );
      }
      setPage(store, key, { status: 'ready', code: null, earlier: false, mark });
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
      // the boundary moves down unless it was raised meanwhile (an eviction above it: the page lies below a hole, the
      // next "Earlier" reads from the raised boundary)
      if (store.getState().pages[key]!.before === known.before) setPage(store, key, { before: page.nextBefore });
      await context.ingest(list.org, list.scope, page.events);
      setPage(store, key, { earlier: false });
    } catch (error) {
      if (store.generation === generation) setPage(store, key, { earlier: false, code: codeOf(error) });
    }
  });
}
