// Pulls of encrypted events for the task screens (03-api.md section 4.4, `GET /v1/scopes/{scope}/events`): the live
// channel starts a new scope at its head (`hello`), so events written before – task titles (`task.meta`), the
// profiles of the org (`org.profile`, `wg.profile`, `member.profile`), the prompts of a task – are pulled once: the
// newest page of the query (`before`, ADR-086 section 1; never every page), read by the event reader like streamed
// ones (never trusted unverified).
//
// `pullOnce` shares one pull per query and store generation (a sign-out starts over); a failed pull is forgotten so
// that the next caller tries again.
//
// `TasksContext` is what the task flows (`core/tasks/*`) need of the signed-in browser: the store, the signed client,
// the log job of an org (refresh after `seq_conflict`) and the event reader (pulled events enter the store exactly like
// streamed ones, 04-crypto.md section 5.12). `AppSession.tasks` provides it; tests pass doubles.
import { eventsBefore, NEWEST, PAGE_MAX } from '../net/pages';
import type { SignedClient } from '../net/signedFetch';
import type { StoredEvent } from '../net/sse';
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

export interface PullQuery {
  readonly kinds: readonly string[];
  /** Only the events of this task. */
  readonly task?: string;
  /** Only the events with this `ref` (an operation's events, KOR-39). */
  readonly ref?: string;
  /** Pages (newest first) to read at most (default 1). */
  readonly pages?: number;
}

/**
 * Reads the newest page of the query (`PAGE_MAX` events; `pages` pages at most, newest first) and hands it to the
 * reader; returns the number of events read. Never every page (ADR-086 section 1): lists that need older events page
 * with `core/tasks/pages.ts`.
 */
export async function pullEvents(
  context: TasksContext,
  org: string,
  scopeId: string,
  query: PullQuery,
): Promise<number> {
  const filter = {
    kinds: query.kinds,
    ...(query.task !== undefined && { task: query.task }),
    ...(query.ref !== undefined && { ref: query.ref }),
  };
  let before: number | null = NEWEST;
  let count = 0;
  for (let page = 0; page < (query.pages ?? 1) && before !== null; page += 1) {
    const read = await eventsBefore(context.client, scopeId, filter, before, PAGE_MAX);
    await context.ingest(org, scopeId, read.events);
    count += read.events.length;
    before = read.nextBefore;
  }
  return count;
}

const shared = new WeakMap<object, Map<string, Promise<number>>>();

/** `pullEvents` once per query and store generation (concurrent callers share the pull). */
export function pullOnce(context: TasksContext, org: string, scopeId: string, query: PullQuery): Promise<number> {
  const store = context.store;
  const pulls = shared.get(store) ?? new Map<string, Promise<number>>();
  shared.set(store, pulls);
  const key = `${store.generation} ${org} ${scopeId} ${query.kinds.join(',')} ${query.task ?? ''} ${query.ref ?? ''} ${query.pages ?? 1}`;
  const known = pulls.get(key);
  if (known !== undefined) return known;
  const pull = pullEvents(context, org, scopeId, query).catch((error: unknown) => {
    pulls.delete(key);
    throw error;
  });
  pulls.set(key, pull);
  return pull;
}
