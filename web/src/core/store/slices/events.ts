// Slice `events`: per scope the events the client holds (06-web.md section 2.3, ADR-063 section 1): an LRU bounded per
// scope by count (`MAX_EVENTS`) and by bytes (`MAX_BYTES`: the event's ciphertext plus its decrypted body), in two
// classes with a bound each – the bulk kinds of sessions (`BULK_KINDS`: output, tool calls, files, hooks, terminal,
// usage) and everything else (task titles, whiteboards, profiles, prompts, operations) – so that chat traffic never
// evicts titles or whiteboards. Each entry keeps the signed event as received and its reader state: `pending` (not checked yet), `locked`
// (signature and rights verified, the epoch key is not available yet), `verified` (with the decrypted body, kept only
// here in memory) or `unverifiable` (with the error code; shown as a neutral row, never as content, never dropped).
import type { Fields } from '../../crypto/fields';
import { isRecord } from '../../registry';
import { BULK_KINDS } from '../focus';
import type { Slice, Store } from '../store';

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

/** Evicts the least recently used entries of each class over its count or byte bound (one pass in LRU order). */
function bounded(entries: Map<number, EventEntry>): Map<number, EventEntry> {
  const totals = { bulk: { count: 0, bytes: 0 }, other: { count: 0, bytes: 0 } };
  for (const entry of entries.values()) {
    const total = totals[classOf(entry)];
    total.count += 1;
    total.bytes += sizeOf(entry);
  }
  const over = (total: { count: number; bytes: number }) => total.count > MAX_EVENTS || total.bytes > MAX_BYTES;
  if (!over(totals.bulk) && !over(totals.other)) return entries;
  for (const [seq, entry] of entries) {
    const total = totals[classOf(entry)];
    if (!over(total)) continue;
    entries.delete(seq);
    total.count -= 1;
    total.bytes -= sizeOf(entry);
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

/** Removes the entries of a scope that match `predicate` (a view drops what lies before a hole, ADR-063). */
export function dropEvents(store: Store, scopeId: string, predicate: (entry: EventEntry) => boolean): void {
  store.update('events', (events) => {
    const scope = events[scopeId];
    if (scope === undefined) return events;
    const entries = new Map([...scope.entries].filter(([, entry]) => !predicate(entry)));
    return entries.size === scope.entries.size ? events : { ...events, [scopeId]: { entries } };
  });
}

/** Every entry of every scope that matches `predicate`. */
export function findEvents(events: EventsState, predicate: (entry: EventEntry) => boolean): EventEntry[] {
  return Object.values(events).flatMap((scope) => [...scope.entries.values()].filter(predicate));
}
