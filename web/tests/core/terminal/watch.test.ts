// The watcher of one terminal: the newest page on open (read without storage), live chunks from the `events` slice in
// strict order, a bounded pull before rendering past a possible gap (throttled, one at a time; a long gap reads the
// newest page again), the registration as a shown session with its refresh, retries after failures and blocks, the
// history window with earlier pages on demand, and the release of the watch.
import { describe, expect, it, vi } from 'vitest';
import type { StoredEvent } from '../../../src/core/net/sse';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore, type Store } from '../../../src/core/store';
import { isShown, refreshShown } from '../../../src/core/store/focus';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import {
  PAGE_LIMIT,
  PULL_GAP_MS,
  REPAIR_LIMIT,
  REPAIR_PAGES,
  RETRY_MS,
  type TerminalContext,
  TerminalWatch,
  terminalTarget,
} from '../../../src/core/terminal/watch';
import { ManualTimers, settle } from '../net/stream';
import { entry, other, ORG, OTHER, SESSION, TARGET, WG } from './helpers';

/** The hub's terminal history: `seq` → entry the reader returns; `head` of the scope. */
class History {
  readonly chunks = new Map<number, EventEntry>();
  head = 0;
  pageSize = REPAIR_LIMIT;
  fail: HubError | Error | null = null;
  odd = false;
  readonly targets: string[] = [];

  request = async (method: string, target: string): Promise<unknown> => {
    this.targets.push(target);
    expect(method).toBe('GET');
    if (this.fail !== null) {
      const error = this.fail;
      this.fail = null;
      throw error;
    }
    if (this.odd) return { events: 'x' };
    const params = new URL(target, 'https://hub.example').searchParams;
    const seqs = [...this.chunks.keys()].sort((a, b) => a - b);
    if (params.has('before')) {
      // newest first below `before`
      const before = Number(params.get('before'));
      const older = seqs.filter((seq) => seq < before).reverse();
      const page = older.slice(0, Math.min(this.pageSize, Number(params.get('limit'))));
      return {
        events: page.map((seq) => ({ scope_seq: seq, received_at: null, event: { seq } })),
        next_before: older.length > page.length ? page.at(-1)! : null,
        head: this.head,
      };
    }
    const after = Number(params.get('after'));
    const found = seqs.filter((seq) => seq > after);
    const page = found.slice(0, this.pageSize);
    const more = found.length > page.length;
    return {
      events: page.map((seq) => ({ scope_seq: seq, received_at: null, event: { seq } })),
      next_after: more ? page.at(-1)! : null,
      head: this.head,
    };
  };

  read = async (_org: string, _scope: string, items: readonly StoredEvent[]): Promise<EventEntry[]> =>
    items.map((item) => this.chunks.get(item.scope_seq)!);
}

function setup() {
  const store = createStore();
  const timers = new ManualTimers();
  const history = new History();
  const released = vi.fn();
  const watch = vi.fn((_org: string, _session: string) => released);
  const context: { -readonly [K in keyof TerminalContext]: TerminalContext[K] } = {
    store,
    client: { request: history.request } as never,
    read: vi.fn(history.read),
    watch,
    timers,
    now: () => timers.now,
  };
  const watcher = new TerminalWatch(context, TARGET);
  const state = () => store.getState().terminal.sessions[SESSION];
  const applied = () => state()?.chunks.map((chunk) => chunk.seq);
  return { store, timers, history, watch, released, context, watcher, state, applied };
}

const live = (store: Store, ...entries: EventEntry[]) => putEvents(store, WG, entries);
const param = (target: string, name: string) => new URL(target, 'https://h').searchParams.get(name);

describe('terminal watch', () => {
  it('builds the repair target', () => {
    expect(terminalTarget(TARGET, 5)).toBe(
      `/v1/scopes/${WG}/events?after=5&limit=${REPAIR_LIMIT}&kinds=session.terminal&session=${SESSION}`,
    );
  });

  it('watches, reads the newest page only and applies live chunks in order', async () => {
    const t = setup();
    t.history.chunks.set(2, entry(2));
    t.history.chunks.set(5, entry(5));
    t.history.chunks.set(7, entry(7));
    t.history.head = 9;
    t.history.pageSize = 2;
    t.watcher.start();
    expect(t.watch).toHaveBeenCalledWith(ORG, SESSION);
    expect(isShown(t.store, undefined, SESSION)).toBe(true);
    expect(t.state()!.status).toBe('loading');
    await settle();
    expect(t.history.targets).toHaveLength(1);
    expect(param(t.history.targets[0]!, 'limit')).toBe(String(PAGE_LIMIT));
    expect(param(t.history.targets[0]!, 'after')).toBeNull();
    expect(t.applied()).toEqual([5, 7]);
    expect(t.state()).toMatchObject({ through: 9, earlier: 5 });
    // the next live chunk follows an accounted event: applied at once without a pull
    live(t.store, other(10), entry(11));
    expect(t.applied()).toEqual([5, 7, 11]);
    // the same entry again is not handed over twice
    live(t.store, other(12));
    expect(t.applied()).toEqual([5, 7, 11]);
    expect(t.history.targets).toHaveLength(1);
    t.watcher.stop();
    expect(t.released).toHaveBeenCalledOnce();
    expect(isShown(t.store, undefined, SESSION)).toBe(false);
    expect(t.state()).toBeUndefined();
    live(t.store, entry(13));
    expect(t.state()).toBeUndefined();
  });

  it('records a pulled chunk of another session as bad instead of showing it', async () => {
    const t = setup();
    t.history.chunks.set(1, entry(1));
    t.history.chunks.set(2, entry(2, { session: OTHER }));
    t.history.head = 2;
    t.watcher.start();
    await settle();
    expect(t.applied()).toEqual([1]);
    expect(t.state()!.bad).toEqual([{ seq: 2, code: 'header_invalid' }]);
    t.watcher.stop();
  });

  it('pulls a gap before rendering further, at most once per interval', async () => {
    const t = setup();
    t.history.head = 3;
    t.watcher.start();
    await settle();
    expect(t.state()!.through).toBe(3);
    t.timers.now = 100;
    // seq 4 is missing (a chunk of another session, or a lost frame): 5 waits
    t.history.chunks.set(5, entry(5));
    t.history.head = 5;
    live(t.store, entry(5));
    expect(t.applied()).toEqual([]);
    expect(t.timers.pending).toEqual([PULL_GAP_MS - 100]);
    // a further chunk while the pull is due does not schedule a second one
    t.history.chunks.set(6, entry(6));
    live(t.store, entry(6));
    expect(t.timers.pending).toHaveLength(1);
    t.timers.advance(PULL_GAP_MS);
    await settle();
    expect(t.history.targets).toHaveLength(2);
    expect(t.applied()).toEqual([5, 6]);
    t.watcher.stop();
  });

  it('repeats a pull when a gap appears during one', async () => {
    const s = setup();
    s.history.head = 2;
    s.watcher.start();
    await settle();
    // a slow second pull: a gap during it asks for one more
    s.timers.now = 5000;
    let release: () => void = () => undefined;
    const slow = s.history.request;
    s.context.client.request = (async (method: string, target: string) => {
      const answer = await slow(method, target);
      await new Promise<void>((done) => (release = done));
      return answer;
    }) as never;
    s.history.chunks.set(4, entry(4));
    s.history.head = 4;
    live(s.store, entry(4));
    s.timers.advance(0);
    await settle();
    expect(s.history.targets).toHaveLength(2);
    live(s.store, entry(6));
    expect(s.timers.pending).toEqual([]);
    s.history.chunks.set(6, entry(6));
    s.history.head = 6;
    release();
    await settle();
    // seq 4 came with the pull; 6 still has a gap (5): one more pull, 1 s after the last one
    expect(s.applied()).toEqual([4]);
    expect(s.timers.pending).toEqual([PULL_GAP_MS]);
    s.context.client.request = slow as never;
    s.timers.advance(PULL_GAP_MS);
    await settle();
    expect(s.applied()).toEqual([4, 6]);
    s.watcher.stop();
  });

  it('shows a failed pull and tries again later; an odd answer counts as unavailable', async () => {
    const t = setup();
    t.history.fail = new HubError('rate_limited', 429);
    t.watcher.start();
    await settle();
    expect(t.state()).toMatchObject({ status: 'error', code: 'rate_limited' });
    expect(t.timers.pending).toEqual([RETRY_MS]);
    t.history.odd = true;
    t.timers.advance(RETRY_MS);
    await settle();
    expect(t.state()).toMatchObject({ status: 'error', code: 'unavailable' });
    t.history.odd = false;
    t.history.fail = new Error('network');
    t.timers.advance(RETRY_MS);
    await settle();
    expect(t.state()).toMatchObject({ status: 'error', code: 'unavailable' });
    t.timers.advance(RETRY_MS);
    await settle();
    expect(t.state()).toMatchObject({ status: 'ready', code: null });
    t.watcher.stop();
    expect(t.timers.pending).toEqual([]);
  });

  it('counts an odd repair answer as unavailable and drops a repair that ends after stop', async () => {
    const t = setup();
    t.history.head = 1;
    t.watcher.start();
    await settle();
    t.timers.now = 5000;
    t.history.odd = true;
    t.watcher.refresh();
    t.timers.advance(0);
    await settle();
    expect(t.state()).toMatchObject({ status: 'error', code: 'unavailable' });
    t.history.odd = false;
    let release: () => void = () => undefined;
    t.context.read = vi.fn(async (...args: Parameters<History['read']>) => {
      await new Promise<void>((done) => (release = done));
      return t.history.read(...args);
    });
    t.timers.advance(RETRY_MS);
    await settle();
    expect(param(t.history.targets.at(-1)!, 'after')).toBe('1');
    t.watcher.stop();
    release();
    await settle();
    expect(t.state()).toBeUndefined();
    expect(t.timers.pending).toEqual([]);
  });

  it('pulls a chunk that could not be read yet again after a while', async () => {
    const t = setup();
    t.history.chunks.set(1, entry(1, { status: 'locked' }));
    t.history.chunks.set(2, entry(2));
    t.history.head = 2;
    t.watcher.start();
    await settle();
    expect(t.state()!.blockedAt).toBe(1);
    expect(t.history.targets).toHaveLength(1);
    expect(t.timers.pending).toEqual([RETRY_MS]);
    t.history.chunks.set(1, entry(1));
    t.timers.advance(RETRY_MS);
    await settle();
    expect(param(t.history.targets[1]!, 'after')).toBe('0');
    expect(t.applied()).toEqual([1, 2]);
    t.watcher.stop();
  });

  it('reads the newest page again when a gap is longer than the repair pages', async () => {
    const t = setup();
    t.history.chunks.set(1, entry(1));
    t.history.head = 1;
    t.watcher.start();
    await settle();
    expect(t.applied()).toEqual([1]);
    expect(t.state()!.epoch).toBe(0);
    // far more chunks than the repair reads arrive while the live channel lost them
    for (let seq = 2; seq <= 20; seq += 1) t.history.chunks.set(seq, entry(seq));
    t.history.head = 21;
    t.history.pageSize = 2;
    t.timers.now = 5000;
    live(t.store, entry(22));
    t.timers.advance(0);
    await settle();
    const afters = t.history.targets.slice(1).map((target) => param(target, 'after'));
    expect(afters).toEqual(['1', '3', '5', null]);
    expect(REPAIR_PAGES).toBe(3);
    // the newest page (2 chunks of this double) replaced the tail: the output before it stays on the hub
    expect(t.applied()).toEqual([19, 20, 22]);
    expect(t.state()).toMatchObject({ epoch: 1, earlier: 19, through: 22 });
    t.watcher.stop();
  });

  it('pulls after a resync of the live channel and not after stop', async () => {
    const t = setup();
    t.history.head = 1;
    t.watcher.start();
    await settle();
    t.history.chunks.set(3, entry(3));
    t.history.head = 3;
    t.timers.now = 5000;
    refreshShown(t.store);
    expect(t.timers.pending).toEqual([0]);
    t.timers.advance(0);
    await settle();
    expect(param(t.history.targets[1]!, 'after')).toBe('1');
    expect(t.applied()).toEqual([3]);
    t.watcher.stop();
    t.watcher.refresh();
    expect(t.timers.pending).toEqual([]);
  });

  it('ends a pull quietly when the session left the store (sign-out)', async () => {
    const t = setup();
    t.watcher.start();
    await settle();
    t.store.reset();
    t.timers.now = 5000;
    t.watcher.refresh();
    t.timers.advance(0);
    await settle();
    expect(t.state()).toBeUndefined();
    expect(t.timers.pending).toEqual([]);
    t.watcher.stop();
  });

  it('loads earlier pages into the history window on demand', async () => {
    const t = setup();
    for (const seq of [1, 2, 3, 4, 5]) t.history.chunks.set(seq, entry(seq));
    t.history.head = 5;
    t.history.pageSize = 2;
    t.watcher.start();
    await settle();
    const window = () => t.state()!.history;
    const windowSeqs = () => window()!.chunks.map((chunk) => chunk.seq);
    // without a window there is nothing to load
    await t.watcher.loadEarlier();
    expect(t.history.targets).toHaveLength(1);
    t.watcher.history();
    expect(windowSeqs()).toEqual([4, 5]);
    const loading = t.watcher.loadEarlier();
    expect(window()!.status).toBe('loading');
    // a second request while one loads does nothing
    await t.watcher.loadEarlier();
    await loading;
    expect(param(t.history.targets[1]!, 'before')).toBe('4');
    expect(windowSeqs()).toEqual([2, 3, 4, 5]);
    // a failure is shown and can be tried again
    t.history.fail = new HubError('rate_limited', 429);
    await t.watcher.loadEarlier();
    expect(window()).toMatchObject({ status: 'error', code: 'rate_limited' });
    t.history.fail = new Error('network');
    await t.watcher.loadEarlier();
    expect(window()).toMatchObject({ status: 'error', code: 'unavailable' });
    await t.watcher.loadEarlier();
    expect(windowSeqs()).toEqual([1, 2, 3, 4, 5]);
    expect(window()!.before).toBeNull();
    // at the start of the recording nothing more is loaded
    await t.watcher.loadEarlier();
    expect(t.history.targets).toHaveLength(5);
    t.watcher.follow();
    expect(window()).toBeNull();
    t.watcher.stop();
    await t.watcher.loadEarlier();
    expect(t.history.targets).toHaveLength(5);
  });

  it('drops an earlier page that arrives after stop or a sign-out', async () => {
    for (const end of ['stop', 'reset', 'fail'] as const) {
      const t = setup();
      for (const seq of [1, 2, 3]) t.history.chunks.set(seq, entry(seq));
      t.history.head = 3;
      t.history.pageSize = 1;
      t.watcher.start();
      await settle();
      t.watcher.history();
      let release: () => void = () => undefined;
      const request = t.history.request;
      if (end === 'fail') t.history.fail = new HubError('unavailable', 503);
      t.context.client.request = (async (method: string, target: string) => {
        await new Promise<void>((done) => (release = done));
        return request(method, target);
      }) as never;
      const loading = t.watcher.loadEarlier();
      await settle();
      if (end === 'reset') t.store.reset();
      else t.watcher.stop();
      release();
      await loading;
      expect(t.state()).toBeUndefined();
    }
  });

  it('drops the results of a pull that ends after stop or a sign-out', async () => {
    for (const end of ['stop', 'reset'] as const) {
      const t = setup();
      t.history.chunks.set(1, entry(1));
      t.history.head = 1;
      let release: () => void = () => undefined;
      t.context.read = vi.fn(async (...args: Parameters<History['read']>) => {
        await new Promise<void>((done) => (release = done));
        return t.history.read(...args);
      });
      t.watcher.start();
      await settle();
      if (end === 'stop') t.watcher.stop();
      else t.store.reset();
      release();
      await settle();
      expect(t.state()).toBeUndefined();
    }
    for (const end of ['stop', 'reset'] as const) {
      const t = setup();
      t.history.fail = new HubError('unavailable', 503);
      let release: () => void = () => undefined;
      const request = t.history.request;
      t.context.client.request = (async (method: string, target: string) => {
        await new Promise<void>((done) => (release = done));
        return request(method, target);
      }) as never;
      t.watcher.start();
      await settle();
      if (end === 'stop') t.watcher.stop();
      else t.store.reset();
      release();
      await settle();
      expect(t.state()).toBeUndefined();
      expect(t.timers.pending).toEqual([]);
    }
  });

  it('uses the system timers by default', () => {
    const t = setup();
    const { timers: _timers, ...rest } = t.context;
    const watcher = new TerminalWatch(rest, TARGET);
    watcher.start();
    watcher.stop();
    expect(t.state()).toBeUndefined();
  });

  it('does not schedule after stop', async () => {
    const t = setup();
    t.watcher.start();
    await settle();
    t.watcher.stop();
    // a store change after stop is ignored
    live(t.store, entry(9));
    expect(t.timers.pending).toEqual([]);
    const s = setup();
    s.history.head = 1;
    s.watcher.start();
    await settle();
    live(s.store, entry(5));
    expect(s.timers.pending).toHaveLength(1);
    s.watcher.stop();
    expect(s.timers.pending).toEqual([]);
  });
});
