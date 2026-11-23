// Slice `events`: per scope the events the client holds (06-web.md section 2.3): a bounded LRU of `MAX_EVENTS` per
// scope. Each entry keeps the signed event as received and its reader state: `pending` (not checked yet), `locked`
// (signature and rights verified, the epoch key is not available yet), `verified` (with the decrypted body, kept only
// here in memory) or `unverifiable` (with the error code; shown as a neutral row, never as content, never dropped).
import type { Fields } from '../../crypto/fields';
import type { Slice, Store } from '../store';

export const MAX_EVENTS = 5000;

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
}

/** Entries by `scope_seq`; the map order is the LRU order (least recently written or touched first). */
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

function bounded(entries: Map<number, EventEntry>): Map<number, EventEntry> {
  for (const seq of entries.keys()) {
    if (entries.size <= MAX_EVENTS) break;
    entries.delete(seq);
  }
  return entries;
}

/** Inserts or replaces entries of one scope (each becomes most recently used). */
export function putEvents(store: Store, scopeId: string, items: readonly EventEntry[]): void {
  if (items.length === 0) return;
  store.update('events', (events) => {
    const entries = new Map(events[scopeId]?.entries);
    for (const item of items) {
      entries.delete(item.scopeSeq);
      entries.set(item.scopeSeq, item);
    }
    return { ...events, [scopeId]: { entries: bounded(entries) } };
  });
}

/** Marks entries as used (a screen shows them), so the LRU keeps them longest. */
export function touchEvents(store: Store, scopeId: string, seqs: readonly number[]): void {
  store.update('events', (events) => {
    const scope = events[scopeId];
    if (scope === undefined) return events;
    const entries = new Map(scope.entries);
    for (const seq of seqs) {
      const entry = entries.get(seq);
      if (entry === undefined) continue;
      entries.delete(seq);
      entries.set(seq, entry);
    }
    return { ...events, [scopeId]: { entries } };
  });
}

/** Every entry of every scope that matches `predicate`. */
export function findEvents(events: EventsState, predicate: (entry: EventEntry) => boolean): EventEntry[] {
  return Object.values(events).flatMap((scope) => [...scope.entries.values()].filter(predicate));
}
