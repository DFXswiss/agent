// Newest-first pages of a scope's events (ADR-063 section 2): the `before` request, its checks, and the old-hub
// fallback (one repeat without `before`, remembered per client, then bounded ascending windows).
import { describe, expect, it } from 'vitest';
import {
  eventsBefore,
  eventsTarget,
  lacksBefore,
  NEWEST,
  OLD_SPAN,
  OLD_WINDOWS,
  PAGE_MAX,
} from '../../../src/core/net/pages';
import { HubError } from '../../../src/core/net/signedFetch';

const item = (seq: number) => ({ scope_seq: seq, received_at: null, event: { header: {} } });

/** A hub of `seqs` that matches the query; `old` refuses `before` like a hub before ADR-063. */
class Hub {
  readonly targets: string[] = [];
  constructor(
    readonly seqs: number[],
    readonly head: number,
    readonly old = false,
  ) {}

  request = async (_method: string, target: string): Promise<unknown> => {
    this.targets.push(target);
    const params = new URL(target, 'https://hub.example').searchParams;
    const limit = Number(params.get('limit'));
    if (params.has('before')) {
      if (this.old) throw new HubError('request_invalid', 400);
      const before = Number(params.get('before'));
      const found = this.seqs.filter((seq) => seq < before).sort((a, b) => b - a);
      const page = found.slice(0, limit);
      return {
        events: page.map(item),
        next_before: found.length > page.length ? page.at(-1)! : null,
        head: this.head,
      };
    }
    const after = Number(params.get('after'));
    const found = this.seqs.filter((seq) => seq > after).sort((a, b) => a - b);
    const page = found.slice(0, limit);
    return { events: page.map(item), next_after: found.length > page.length ? page.at(-1)! : null, head: this.head };
  };
}

const seqsOf = (page: { events: { scope_seq: number }[] }) => page.events.map((event) => event.scope_seq);

describe('eventsBefore', () => {
  it('builds the targets of both directions with every filter', () => {
    expect(eventsTarget('s 1', { kinds: ['a', 'b'], task: 't', session: 's', ref: 'r' }, { before: 9 }, 50)).toBe(
      '/v1/scopes/s%201/events?before=9&limit=50&kinds=a%2Cb&task=t&session=s&ref=r',
    );
    expect(eventsTarget('s', {}, { after: 3 }, 1000)).toBe('/v1/scopes/s/events?after=3&limit=1000');
  });

  it('reads the newest page and older ones, ascending, below `before`', async () => {
    const hub = new Hub([1, 2, 3, 4, 5], 5);
    const newest = await eventsBefore(hub, 'wg', {}, undefined, 2);
    expect([seqsOf(newest), newest.nextBefore, newest.head]).toEqual([[4, 5], 4, 5]);
    const older = await eventsBefore(hub, 'wg', {}, 4, 2);
    expect([seqsOf(older), older.nextBefore]).toEqual([[2, 3], 2]);
    expect(hub.targets[0]).toBe(eventsTarget('wg', {}, { before: NEWEST }, 2));
    expect(lacksBefore(hub)).toBe(false);
    // a hub's event at or above `before` is not taken; a missing head is 0
    const odd = {
      request: async () => ({ events: [item(7), { ...item(3), received_at: undefined }], next_before: null }),
    };
    const page = await eventsBefore(odd, 'wg', {}, 5);
    expect([seqsOf(page), page.head, page.events[0]!.received_at]).toEqual([[3], 0, null]);
  });

  it('refuses malformed answers and passes other errors on', async () => {
    for (const body of [
      null,
      { events: 'x', next_before: null },
      { events: [{ scope_seq: -1, event: {} }], next_before: null },
      { events: [{ scope_seq: 1, received_at: 1, event: {} }], next_before: null },
      { events: [{ scope_seq: 1, received_at: null, event: 'x' }], next_before: null },
      { events: [], next_before: 'x' },
      { events: [] },
    ]) {
      await expect(eventsBefore({ request: async () => body }, 'wg', {})).rejects.toMatchObject({
        code: 'unavailable',
      });
    }
    const failing = {
      request: async () => {
        throw new HubError('forbidden', 403);
      },
    };
    await expect(eventsBefore(failing, 'wg', {})).rejects.toMatchObject({ code: 'forbidden' });
    expect(lacksBefore(failing)).toBe(false);
  });

  it('on an old hub repeats once without `before`, remembers it and reads a small scope in one page', async () => {
    const hub = new Hub([1, 2, 3], 3, true);
    const page = await eventsBefore(hub, 'wg', { kinds: ['prompt'] }, NEWEST, 2);
    expect([seqsOf(page), page.nextBefore, page.head]).toEqual([[2, 3], 2, 3]);
    expect(hub.targets).toEqual([
      eventsTarget('wg', { kinds: ['prompt'] }, { before: NEWEST }, 2),
      eventsTarget('wg', { kinds: ['prompt'] }, { after: 0 }, PAGE_MAX),
    ]);
    expect(lacksBefore(hub)).toBe(true);
    const rest = await eventsBefore(hub, 'wg', { kinds: ['prompt'] }, 2, 2);
    expect([seqsOf(rest), rest.nextBefore]).toEqual([[1], null]);
    expect(hub.targets[2]).toBe(eventsTarget('wg', { kinds: ['prompt'] }, { after: 0 }, PAGE_MAX));
  });

  it('on an old hub reads a large scope in bounded windows below `before`', async () => {
    const seqs = Array.from({ length: 6000 }, (_, i) => i + 1);
    const hub = new Hub(seqs, 6000, true);
    const page = await eventsBefore(hub, 'wg', {}, NEWEST, 200);
    expect(seqsOf(page)).toEqual(seqs.slice(-200));
    expect(page.nextBefore).toBe(5801);
    // more than `limit` in the window: the oldest kept event is the next `before`; requests stay bounded
    expect(hub.targets.length).toBeLessThanOrEqual(2 + Math.ceil(OLD_SPAN / PAGE_MAX) + 1);
    // a sparse query: the window is widened at most OLD_WINDOWS times while it holds fewer than `limit` events
    const early = Array.from({ length: 1001 }, (_, i) => i + 1);
    const sparse = new Hub(early, 20_000, true);
    const none = await eventsBefore(sparse, 'wg', { task: 't' }, NEWEST, 200);
    expect([seqsOf(none), none.nextBefore]).toEqual([[], 20_001 - OLD_WINDOWS * OLD_SPAN]);
    const found = await eventsBefore(sparse, 'wg', { task: 't' }, none.nextBefore!, 200);
    expect([seqsOf(found), found.nextBefore]).toEqual([early.slice(-200), 802]);
    // a window reaching the start ends with nothing older; a page beyond `before` ends a window
    const whole = await eventsBefore(new Hub(early, 1001, true), 'wg', {}, NEWEST, 1500);
    expect([seqsOf(whole).length, whole.nextBefore]).toEqual([1001, null]);
    const middle = await eventsBefore(new Hub(seqs.slice(0, 3000), 3000, true), 'wg', {}, 1500, 200);
    expect([seqsOf(middle)[0], seqsOf(middle).at(-1), middle.nextBefore]).toEqual([1300, 1499, 1300]);
    const first = await eventsBefore(new Hub(seqs, 6000, true), 'wg', {}, 1, 200);
    expect([seqsOf(first), first.nextBefore]).toEqual([[], null]);
  });

  it('takes an ascending answer to `before` as an old hub that ignored it', async () => {
    const targets: string[] = [];
    const ignoring = {
      request: async (_method: string, target: string) => {
        targets.push(target);
        return { events: [item(2), item(1)], next_after: null, head: 2 };
      },
    };
    const page = await eventsBefore(ignoring, 'wg', {}, NEWEST, 1);
    expect([seqsOf(page), page.nextBefore, targets.length]).toEqual([[2], 2, 1]);
    expect(lacksBefore(ignoring)).toBe(true);
    // an answer without a head: the newest event it holds is the head
    const headless = { request: async () => ({ events: [item(4)], next_after: null }) };
    const all = await eventsBefore(headless, 'wg', {}, NEWEST, 5);
    expect([seqsOf(all), all.head]).toEqual([[4], 4]);
  });

  it('on an old hub refuses malformed ascending pages', async () => {
    for (const body of [
      null,
      { events: 'x', next_after: null, head: 0 },
      { events: [], next_after: 'x', head: 0 },
      { events: [], next_after: null, head: 'x' },
    ]) {
      const client = {
        request: async (_method: string, target: string) => {
          if (target.includes('before=')) throw new HubError('request_invalid', 400);
          return body;
        },
      };
      await expect(eventsBefore(client, 'wg', {})).rejects.toMatchObject({ code: 'unavailable' });
    }
  });
});
