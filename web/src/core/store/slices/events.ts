// Slice `events`: per scope the events the client holds (06-web.md section 2.3, ADR-086 section 1), bounded per scope
// by count (`MAX_EVENTS`) and by bytes (`MAX_BYTES`: the event's ciphertext plus its decrypted body), in two classes
// with a bound each – the bulk kinds of sessions (`BULK_KINDS`: output, tool calls, files, hooks, terminal, usage) and
// everything else (task titles, whiteboards, profiles, prompts, operations) – so that chat traffic never evicts titles
// or whiteboards. Each entry keeps the signed event as received and its reader state: `pending` (not checked yet),
// `locked` (signature and rights verified, the epoch key is not available yet), `verified` (with the decrypted body,
// kept only here in memory) or `unverifiable` (with the error code; shown as a neutral row, never as content, never
// dropped by a view).
//
// Eviction never leaves a silent gap: a class over its bound loses its **oldest** events (lowest `scope_seq`, the far
// end from the newest page every view opens with and from live inflow), and every paged list (`slices/pages.ts`) that
// held an evicted or dropped event at or above its boundary gets its boundary raised above it – the view shows only
// what lies above the boundary and "Earlier" reads the rest again (titles alike: `core/tasks/list.ts`). Every mounted
// list (a view shows it, `focus.shownKeys`) keeps its events at or above its boundary – the newest page a view just
// opened as much as the pages it read back with "Earlier": they are evicted only after everything else, and only
// beyond twice the bound (then again oldest first, raising the boundary). So bulk inflow of other tasks never empties
// the chat a view has just opened. `dropEvents` (a view's re-read drops what lies before a hole) never drops what
// another mounted list keeps.
import type { Fields } from '../../crypto/fields';
import { isRecord } from '../../registry';
import { BULK_KINDS, shownKeys } from '../focus';
import type { Slice, Store } from '../store';
import type { ListQuery, PageState } from './pages';

/** Entries per scope and class. */
export const MAX_EVENTS = 5000;
/** Bytes (ciphertext and decrypted body) per scope and class. */
export const MAX_BYTES = 64 * 1024 * 1024;

export type EventStatus = 'pending' | 'locked' | 'verified' | 'unverifiable';

export interface EventEntry {
  /** The org of the channel or pull that delivered the event (the header must name it). */
  readonly org: string;
  readonly scopeId: string;
  readonly scopeSeq: number;
  readonly receivedAt: string | null;
  /** The event object as received from the hub. */
  readonly event: unknown;
  readonly status: EventStatus;
  readonly code: string | null;
  /** The checked header (`locked`, `verified`). */
  readonly header: Fields | null;
  /** The decrypted body (`verified` only). */
  readonly body: unknown;
  /** Bytes of the decrypted body (set by the reader; counts towards `MAX_BYTES`). */
  readonly bodyBytes?: number;
}

/** Entries by `scope_seq` (in the order they were written). */
export interface ScopeEvents {
  readonly entries: ReadonlyMap<number, EventEntry>;
}

export type EventsState = Readonly<Record<string, ScopeEvents>>;

declare module '../store' {
  interface StoreState {
    events: EventsState;
  }
}

export const slice: Slice<'events'> = { name: 'events', initial: () => ({}) };

export type EventClass = 'bulk' | 'other';

/** The kind of an entry: the checked header's, else the raw header's (null if it has none). */
export function kindOf(entry: EventEntry): unknown {
  if (entry.header !== null) return entry.header.kind;
  return isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header.kind : null;
}

export function classOf(entry: EventEntry): EventClass {
  const kind = kindOf(entry);
  return typeof kind === 'string' && BULK_KINDS.has(kind) ? 'bulk' : 'other';
}

/** The bytes an entry holds: its ciphertext (b64u text as received) and its decrypted body. */
export function sizeOf(entry: EventEntry): number {
  const ct = isRecord(entry.event) && typeof entry.event.ct === 'string' ? entry.event.ct.length : 0;
  return ct + (entry.bodyBytes ?? 0);
}

/** Whether a stored entry belongs to a list's query (by its unchecked header: unverifiable rows too). */
export function matchesQuery(entry: EventEntry, query: ListQuery): boolean {
  const header = isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};
  return (
    (query.kinds === undefined || query.kinds.includes(kindOf(entry) as string)) &&
    (query.task === undefined || header.task === query.task) &&
    (query.session === undefined || header.session === query.session) &&
    (query.ref === undefined || header.ref === query.ref)
  );
}

type Pages = readonly (readonly [string, PageState])[];

/** The paged lists of a scope. */
const listsOf = (store: Store, scopeId: string): Pages =>
  Object.entries(store.getState().pages).filter(([, page]) => page.list?.scope === scopeId);

/** Whether a mounted list holds the entry at or above its boundary (module comment). */
function keptBy(store: Store, lists: Pages): (entry: EventEntry) => boolean {
  const shown = shownKeys(store);
  const kept = lists.filter(([key]) => shown.has(key));
  return (entry) =>
    kept.some(([, page]) => entry.scopeSeq >= (page.before ?? 0) && matchesQuery(entry, page.list!.query));
}

/**
 * Evicts the oldest entries of each class over its bound, first those no mounted list keeps, then – only
 * beyond twice the bound – those as well; returns the evicted entries.
 */
function bounded(entries: Map<number, EventEntry>, kept: (entry: EventEntry) => boolean): EventEntry[] {
  const totals = { bulk: { count: 0, bytes: 0 }, other: { count: 0, bytes: 0 } };
  for (const entry of entries.values()) {
    const total = totals[classOf(entry)];
    total.count += 1;
    total.bytes += sizeOf(entry);
  }
  const over = (total: { count: number; bytes: number }, factor: number) =>
    total.count > MAX_EVENTS * factor || total.bytes > MAX_BYTES * factor;
  const removed: EventEntry[] = [];
  if (!over(totals.bulk, 1) && !over(totals.other, 1)) return removed;
  const oldest = [...entries.values()].sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const factor of [1, 2]) {
    for (const entry of oldest) {
      const total = totals[classOf(entry)];
      if (!entries.has(entry.scopeSeq) || !over(total, factor) || (factor === 1 && kept(entry))) continue;
      entries.delete(entry.scopeSeq);
      removed.push(entry);
      total.count -= 1;
      total.bytes -= sizeOf(entry);
    }
  }
  return removed;
}

/** Raises the boundary of every list of the scope above the removed entries it held at or above it (no silent gap). */
function raiseBoundaries(store: Store, lists: Pages, removed: readonly EventEntry[]): void {
  if (removed.length === 0) return;
  const raised: Record<string, PageState> = {};
  for (const [key, page] of lists) {
    let top = -1;
    for (const entry of removed) {
      if (entry.scopeSeq > top && matchesQuery(entry, page.list!.query)) top = entry.scopeSeq;
    }
    if (top >= (page.before ?? 0)) raised[key] = { ...page, before: top + 1 };
  }
  if (Object.keys(raised).length > 0) store.update('pages', (pages) => ({ ...pages, ...raised }));
}

/** Inserts or replaces entries of one scope (bounded, module comment). */
export function putEvents(store: Store, scopeId: string, items: readonly EventEntry[]): void {
  if (items.length === 0) return;
  const lists = listsOf(store, scopeId);
  const kept = keptBy(store, lists);
  let removed: EventEntry[] = [];
  store.update('events', (events) => {
    const entries = new Map(events[scopeId]?.entries);
    for (const item of items) {
      entries.delete(item.scopeSeq);
      entries.set(item.scopeSeq, item);
    }
    removed = bounded(entries, kept);
    return { ...events, [scopeId]: { entries } };
  });
  raiseBoundaries(store, lists, removed);
}

/**
 * Removes the entries of a scope that match `predicate` (a view drops what lies before a hole, ADR-086) and that no
 * mounted list keeps; the lists that held them raise their boundary (module comment).
 */
export function dropEvents(store: Store, scopeId: string, predicate: (entry: EventEntry) => boolean): void {
  const lists = listsOf(store, scopeId);
  const kept = keptBy(store, lists);
  const removed: EventEntry[] = [];
  store.update('events', (events) => {
    const scope = events[scopeId];
    if (scope === undefined) return events;
    const entries = new Map(scope.entries);
    for (const [seq, entry] of scope.entries) {
      if (!predicate(entry) || kept(entry)) continue;
      entries.delete(seq);
      removed.push(entry);
    }
    return removed.length === 0 ? events : { ...events, [scopeId]: { entries } };
  });
  raiseBoundaries(store, lists, removed);
}

/** Every entry of every scope that matches `predicate`. */
export function findEvents(events: EventsState, predicate: (entry: EventEntry) => boolean): EventEntry[] {
  return Object.values(events).flatMap((scope) => [...scope.entries.values()].filter(predicate));
}
