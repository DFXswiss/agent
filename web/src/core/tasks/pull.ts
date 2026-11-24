// Pulls of encrypted events for the task screens (03-api.md section 4.4, `GET /v1/scopes/{scope}/events`): the live
// channel starts a new scope at its head (`hello`), so events written before – task titles (`task.meta`), the
// profiles of the org (`org.profile`, `wg.profile`, `member.profile`), the prompts of a task – are pulled once, page by
// page (`next_after`), and read by the event reader like streamed ones (never trusted unverified).
//
// `pullOnce` shares one pull per query and store generation (a sign-out starts over); a failed pull is forgotten so
// that the next caller tries again.
//
// `TasksContext` is what the task flows (`core/tasks/*`) need of the signed-in browser: the store, the signed client,
// the log job of an org (refresh after `seq_conflict`) and the event reader (pulled events enter the store exactly like
// streamed ones, 04-crypto.md section 5.12). `AppSession.tasks` provides it; tests pass doubles.
import { HubError, type SignedClient } from '../net/signedFetch';
import type { StoredEvent } from '../net/sse';
import { isRecord } from '../registry';
import type { Store } from '../store/store';

export interface TasksContext {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  /** Runs the log verification job of `org`; true when it completed. */
  readonly refreshLog: (org: string) => Promise<boolean>;
  /** Reads pulled events of a scope of `org` into the `events` slice. */
  readonly ingest: (org: string, scopeId: string, items: readonly StoredEvent[]) => Promise<void>;
  readonly now: () => number;
}

export const PAGE_LIMIT = 1000;

export interface PullQuery {
  readonly kinds: readonly string[];
  /** Only the events of this task. */
  readonly task?: string;
  /** Only the events with this `ref` (an operation's events, KOR-39). */
  readonly ref?: string;
}

interface EventsPage {
  readonly events: StoredEvent[];
  readonly next_after: number | null;
}

const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

function isPage(value: unknown): value is EventsPage {
  return (
    isRecord(value) &&
    Array.isArray(value.events) &&
    value.events.every(
      (item) =>
        isRecord(item) &&
        isSeq(item.scope_seq) &&
        (item.received_at === null || typeof item.received_at === 'string') &&
        'event' in item,
    ) &&
    (value.next_after === null || isSeq(value.next_after))
  );
}

/** The request target of one page. */
export function pullTarget(scopeId: string, query: PullQuery, after: number | null): string {
  const params = new URLSearchParams();
  if (after !== null) params.set('after', String(after));
  params.set('limit', String(PAGE_LIMIT));
  params.set('kinds', query.kinds.join(','));
  if (query.task !== undefined) params.set('task', query.task);
  if (query.ref !== undefined) params.set('ref', query.ref);
  return `/v1/scopes/${encodeURIComponent(scopeId)}/events?${params.toString()}`;
}

/** Pulls every page of the query and hands each to the reader; returns the number of events read. */
export async function pullEvents(
  context: TasksContext,
  org: string,
  scopeId: string,
  query: PullQuery,
): Promise<number> {
  let after: number | null = null;
  let count = 0;
  for (;;) {
    const page = await context.client.request('GET', pullTarget(scopeId, query, after));
    if (!isPage(page)) throw new HubError('unavailable', 200);
    await context.ingest(org, scopeId, page.events);
    count += page.events.length;
    if (page.next_after === null) return count;
    after = page.next_after;
  }
}

const shared = new WeakMap<object, Map<string, Promise<number>>>();

/** `pullEvents` once per query and store generation (concurrent callers share the pull). */
export function pullOnce(context: TasksContext, org: string, scopeId: string, query: PullQuery): Promise<number> {
  const store = context.store;
  const pulls = shared.get(store) ?? new Map<string, Promise<number>>();
  shared.set(store, pulls);
  const key = `${store.generation} ${org} ${scopeId} ${query.kinds.join(',')} ${query.task ?? ''} ${query.ref ?? ''}`;
  const known = pulls.get(key);
  if (known !== undefined) return known;
  const pull = pullEvents(context, org, scopeId, query).catch((error: unknown) => {
    pulls.delete(key);
    throw error;
  });
  pulls.set(key, pull);
  return pull;
}
