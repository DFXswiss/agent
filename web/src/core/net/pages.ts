// Newest-first pages of a scope's events (ADR-086 section 2, 03-api.md section 4.4): `GET /v1/scopes/{scope}/events`
// with `before=<scope_seq>` answers the newest matching events below `before` (newest first, `next_before` when older
// ones exist). The chat, the session chat, the terminal and the operations read the newest page first and go back
// page by page on demand – never every page in a loop.
//
// An old hub does not know `before` and answers it with `400 request_invalid` (or, ignoring it, with an ascending
// page: `next_after` instead of `next_before`). The client then repeats the request once without it (or takes that
// page), remembers for this hub (per signed client, i.e. per loaded configuration) that `before` is not supported,
// and reads the same page with bounded ascending pages instead: a window of `OLD_SPAN` scope seqs below `before`,
// widened at most `OLD_WINDOWS` times while it holds fewer than `limit` matching events. The scope's head is learned
// once per client and scope (the first ascending page) and kept: the newest page then reads only what lies above it,
// an older page needs no head at all. When the windows find nothing although older content exists (a chat whose
// events lie far below the head), the newest match below them is searched by halves (one-event probes, about
// log2(head / OLD_SPAN) requests) and the windows are read once more from there – so an emulated page is empty only
// when nothing older matches (the view shows its loading state meanwhile). The answer has the same shape either way.
// The emulation (`net/old-pages.ts`) is loaded only when a hub turns out to be old: not part of the first load.
import { isRecord } from '../registry';
import { HubError, type SignedClient } from './signedFetch';
import type { StoredEvent } from './sse';

/** `before` of the newest page: above every `scope_seq` (the contract's `q_seq` has up to 16 digits). */
export const NEWEST = Number.MAX_SAFE_INTEGER;
/** Default and largest page of the events endpoint. */
export const PAGE_DEFAULT = 200;
export const PAGE_MAX = 1000;
/** Scope seqs one emulated window covers on an old hub, and how often it is widened. */
export const OLD_SPAN = 2000;
export const OLD_WINDOWS = 5;

export interface EventsQuery {
  readonly kinds?: readonly string[];
  readonly task?: string;
  readonly session?: string;
  /** The operation (`ref`) the events belong to. */
  readonly ref?: string;
}

export interface EventsBefore {
  /** The events in ascending `scope_seq` (the reader settles them in this order). */
  readonly events: StoredEvent[];
  /** `before` of the next older page; null when nothing older matches. */
  readonly nextBefore: number | null;
  /** The scope's head when the hub answered (0 if it did not say). */
  readonly head: number;
}

export type Client = Pick<SignedClient, 'request'>;

export const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

export const isStored = (item: unknown): item is StoredEvent =>
  isRecord(item) &&
  isSeq(item.scope_seq) &&
  (item.received_at === undefined || item.received_at === null || typeof item.received_at === 'string') &&
  isRecord(item.event);

/** The emulation for old hubs (`net/old-pages.ts`, a lazy chunk). */
const oldPages = (): Promise<typeof import('./old-pages')> => import('./old-pages');

/** Hubs (signed clients) that refused `before`. */
const unsupported = new WeakSet<object>();

/** Whether the hub behind `client` is known to lack `before` (old hub). */
export const lacksBefore = (client: Client): boolean => unsupported.has(client);

/** The request target of one page: `before` (newest first) or `after` (ascending). */
export function eventsTarget(
  scopeId: string,
  query: EventsQuery,
  page: { readonly before: number } | { readonly after: number },
  limit: number,
): string {
  const params = new URLSearchParams();
  if ('before' in page) params.set('before', String(page.before));
  else params.set('after', String(page.after));
  params.set('limit', String(limit));
  if (query.kinds !== undefined) params.set('kinds', query.kinds.join(','));
  if (query.task !== undefined) params.set('task', query.task);
  if (query.session !== undefined) params.set('session', query.session);
  if (query.ref !== undefined) params.set('ref', query.ref);
  return `/v1/scopes/${encodeURIComponent(scopeId)}/events?${params.toString()}`;
}

const normal = (item: StoredEvent): StoredEvent => ({
  scope_seq: item.scope_seq,
  received_at: item.received_at ?? null,
  event: item.event,
});

export const ascending = (items: readonly StoredEvent[]): StoredEvent[] =>
  items.map(normal).sort((a, b) => a.scope_seq - b.scope_seq);

/**
 * The newest page of `query` below `before` (default: the newest of all), at most `limit` events. An old hub is
 * served by bounded ascending pages (module comment).
 */
export async function eventsBefore(
  client: Client,
  scopeId: string,
  query: EventsQuery,
  before: number = NEWEST,
  limit: number = PAGE_DEFAULT,
): Promise<EventsBefore> {
  if (!unsupported.has(client)) {
    let body: unknown;
    try {
      body = await client.request('GET', eventsTarget(scopeId, query, { before }, limit));
    } catch (error) {
      if (!(error instanceof HubError && error.code === 'request_invalid')) throw error;
      unsupported.add(client);
      return (await oldPages()).ascendingBefore(client, scopeId, query, before, limit);
    }
    // a hub that ignored `before` answered the first ascending page: it is an old hub as well
    if (isRecord(body) && body.next_before === undefined && 'next_after' in body) {
      unsupported.add(client);
      const old = await oldPages();
      return old.ascendingBefore(client, scopeId, query, before, limit, old.checkedAscending(body));
    }
    if (
      !isRecord(body) ||
      !Array.isArray(body.events) ||
      !body.events.every(isStored) ||
      !(body.next_before === null || isSeq(body.next_before))
    )
      throw new HubError('unavailable', 200);
    const events = ascending(body.events).filter((item) => item.scope_seq < before);
    return { events, nextBefore: body.next_before, head: isSeq(body.head) ? body.head : 0 };
  }
  return (await oldPages()).ascendingBefore(client, scopeId, query, before, limit);
}
