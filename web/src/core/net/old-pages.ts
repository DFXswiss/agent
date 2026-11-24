// `eventsBefore` on an old hub that does not know `before` (the emulation of the module comment of `net/pages.ts`):
// bounded ascending windows below `before`, the head learned once per client and scope, and a search by halves for
// the newest older match. Loaded by `net/pages.ts` only when a hub turns out to be old.
import { isRecord } from '../registry';
import {
  ascending,
  type Client,
  type EventsBefore,
  type EventsQuery,
  eventsTarget,
  isSeq,
  isStored,
  NEWEST,
  OLD_SPAN,
  OLD_WINDOWS,
  PAGE_MAX,
} from './pages';
import { HubError } from './signedFetch';
import type { StoredEvent } from './sse';

export interface AscendingPage {
  readonly events: StoredEvent[];
  readonly next_after: number | null;
  readonly head: number;
}

async function ascendingPage(
  client: Client,
  scopeId: string,
  query: EventsQuery,
  after: number,
): Promise<AscendingPage> {
  return checkedAscending(await client.request('GET', eventsTarget(scopeId, query, { after }, PAGE_MAX)));
}

export function checkedAscending(body: unknown): AscendingPage {
  if (
    !isRecord(body) ||
    !Array.isArray(body.events) ||
    !body.events.every(isStored) ||
    !(body.next_after === null || isSeq(body.next_after)) ||
    !(body.head === undefined || isSeq(body.head))
  )
    throw new HubError('unavailable', 200);
  // the head is at least the newest event the page holds (a hub that did not name it)
  const head = Math.max(body.head ?? 0, ...body.events.map((item) => item.scope_seq));
  return { events: body.events, next_after: body.next_after, head };
}

/** Heads of the scopes per old hub (signed client), as learned from its ascending pages. */
const heads = new WeakMap<object, Map<string, number>>();

/** The newest scope seq of a match below `end`, to within `OLD_SPAN` (null: no match below `end`). */
async function newestBelow(client: Client, scopeId: string, query: EventsQuery, end: number): Promise<number | null> {
  const probe = async (after: number): Promise<number | null> => {
    const page = checkedAscending(await client.request('GET', eventsTarget(scopeId, query, { after }, 1)));
    const seq = page.events[0]?.scope_seq;
    return seq !== undefined && seq < end ? seq : null;
  };
  let found = await probe(0);
  if (found === null) return null;
  // a match lies in [found, high]; nothing matches in (high, end)
  let high = end - 1;
  while (high - found >= OLD_SPAN) {
    const middle = Math.floor((found + high) / 2);
    const seq = await probe(middle);
    if (seq === null) high = middle;
    else found = seq;
  }
  return high;
}

/** `eventsBefore` on an old hub: ascending pages over windows below `before` (bounded, module comment). */
export async function ascendingBefore(
  client: Client,
  scopeId: string,
  query: EventsQuery,
  before: number,
  limit: number,
  known?: AscendingPage,
): Promise<EventsBefore> {
  const cache = heads.get(client) ?? new Map<string, number>();
  heads.set(client, cache);
  const cached = cache.get(scopeId);
  let found: StoredEvent[] = [];
  let end = before;
  if (known !== undefined || before === NEWEST) {
    // the newest page needs the head: the first request reads what lies above the head known so far (from 0 the
    // first time, or the page an old hub answered to `before`); an older page lies below a head already seen
    const from = known === undefined ? (cached ?? 0) : 0;
    const first = known ?? (await ascendingPage(client, scopeId, query, from));
    cache.set(scopeId, Math.max(first.head, cached ?? 0));
    end = Math.min(before, cache.get(scopeId)! + 1);
    if (first.next_after === null) {
      found = ascending(first.events).filter((item) => item.scope_seq > from && item.scope_seq < end);
      // every match lies in this page: a scope whose matches fit one page is done
      if (from === 0) end = 0;
      else end = Math.min(end, from + 1);
    }
  }
  let low = end - 1;
  for (let probed = false; found.length < limit && low > 0; probed = true) {
    for (let window = 0; window < OLD_WINDOWS && found.length < limit && low > 0; window += 1) {
      low = Math.max(0, end - 1 - OLD_SPAN);
      let after = low;
      const part: StoredEvent[] = [];
      for (;;) {
        const page = await ascendingPage(client, scopeId, query, after);
        part.push(...page.events.filter((item) => item.scope_seq > low && item.scope_seq < end));
        if (page.next_after === null || page.next_after >= end - 1) break;
        after = page.next_after;
      }
      found.unshift(...ascending(part));
      end = low + 1;
    }
    // the windows found nothing: the content lies further down – look for the newest match below them (bounded:
    // a search by halves, then the windows once more), so that a page is empty only when nothing older exists
    if (found.length > 0 || low === 0 || probed) break;
    const newest = await newestBelow(client, scopeId, query, end);
    if (newest === null) low = 0;
    else end = newest + 1;
  }
  const kept = found.slice(-limit);
  const nextBefore = found.length > kept.length ? kept[0]!.scope_seq : low > 0 ? low + 1 : null;
  return { events: kept, nextBefore, head: cache.get(scopeId) ?? 0 };
}
