// U-WEB-reader-batch (HOT-05): the reader writes settled entries at most once per frame; `ingest` and `retry` write
// their last batch before they return, the live channel (`defer`) leaves it to the frame timer; a sign-out drops the
// buffer and an entry evicted meanwhile stays evicted.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { StoredEvent } from '../../../src/core/net/sse';
import { createStore, type Store } from '../../../src/core/store';
import { show, skipMark } from '../../../src/core/store/focus';
import { EventReader, FRAME_MS, type ReaderClock } from '../../../src/core/store/reader';
import type { EventEntry } from '../../../src/core/store/slices/events';

const ORG = 'org';
const SCOPE = 'wg';

class Clock implements ReaderClock {
  time = 1000;
  timers: { at: number; callback: () => void; id: number }[] = [];
  #next = 0;
  now = () => this.time;
  setTimeout = (callback: () => void, ms: number) => {
    this.#next += 1;
    this.timers.push({ at: this.time + ms, callback, id: this.#next });
    return this.#next;
  };
  clearTimeout = (handle: unknown) => {
    this.timers = this.timers.filter((timer) => timer.id !== handle);
  };
  advance(ms: number) {
    this.time += ms;
    const due = this.timers.filter((timer) => timer.at <= this.time);
    this.timers = this.timers.filter((timer) => timer.at > this.time);
    for (const timer of due) timer.callback();
  }
}

let store: Store;
let clock: Clock;
let reader: EventReader;
let updates: number;

const stored = (seq: number): StoredEvent => ({ scope_seq: seq, received_at: null, event: { header: { seq } } });

const statuses = () =>
  [...(store.getState().events[SCOPE]?.entries.values() ?? [])].map((entry) => [entry.scopeSeq, entry.status]);

beforeEach(() => {
  store = createStore();
  clock = new Clock();
  reader = new EventReader(store, { trigger: async () => true }, clock);
  vi.spyOn(reader, 'read').mockImplementation(async (entry: EventEntry) => ({
    ...entry,
    status: 'verified',
    header: { id: `id-${entry.scopeSeq}` },
    body: {},
  }));
  updates = 0;
  store.subscribe(() => (updates += 1));
});

describe('batched writes', () => {
  it('writes a whole ingest in two updates and finishes before it returns', async () => {
    await reader.ingest(ORG, SCOPE, [1, 2, 3, 4, 5].map(stored));
    expect(statuses()).toEqual([1, 2, 3, 4, 5].map((seq) => [seq, 'verified']));
    expect(updates).toBe(2);
    expect(clock.timers).toEqual([]);
  });

  it('collects deferred deliveries until the frame timer writes them', async () => {
    for (const seq of [1, 2, 3]) await reader.ingest(ORG, SCOPE, [stored(seq)], { defer: true });
    expect(statuses()).toEqual([]);
    expect(clock.timers).toHaveLength(1);
    clock.advance(0);
    expect(statuses()).toEqual([1, 2, 3].map((seq) => [seq, 'verified']));
    expect(updates).toBe(1);
    // the next batch waits for the rest of the frame
    await reader.ingest(ORG, SCOPE, [stored(4)], { defer: true });
    expect(clock.timers[0]!.at).toBe(clock.time + FRAME_MS);
    clock.advance(FRAME_MS);
    expect(statuses().at(-1)).toEqual([4, 'verified']);
    expect(updates).toBe(2);
    // nothing buffered: a flush writes nothing
    reader.flush();
    expect(updates).toBe(2);
  });

  it('writes the buffered live entries before a direct ingest', async () => {
    await reader.ingest(ORG, SCOPE, [stored(1)], { defer: true });
    await reader.ingest(ORG, SCOPE, [stored(2)]);
    expect(statuses()).toEqual([
      [1, 'verified'],
      [2, 'verified'],
    ]);
    expect(clock.timers).toEqual([]);
  });

  it('drops the buffer of a signed-out store', async () => {
    await reader.ingest(ORG, SCOPE, [stored(1)], { defer: true });
    store.reset();
    clock.advance(FRAME_MS);
    expect(store.getState().events).toEqual({});
  });

  it('leaves an entry evicted before its settled state was written evicted', async () => {
    vi.mocked(reader.read).mockImplementation(async (entry) => {
      store.update('events', () => ({}));
      return { ...entry, status: 'unverifiable', code: 'header_invalid', header: null, body: null };
    });
    await reader.ingest(ORG, SCOPE, [stored(1)]);
    expect(store.getState().events).toEqual({});
  });

  it('writes the entries read again by retry before it returns', async () => {
    vi.mocked(reader.read).mockImplementationOnce(async (entry) => entry);
    await reader.ingest(ORG, SCOPE, [stored(1)]);
    expect(statuses()).toEqual([[1, 'pending']]);
    await reader.retry(ORG);
    expect(statuses()).toEqual([[1, 'verified']]);
  });

  it('lets retry see the live entries still waiting in the buffer', async () => {
    vi.mocked(reader.read).mockImplementationOnce(async (entry) => ({ ...entry, status: 'locked', header: {} }));
    await reader.ingest(ORG, SCOPE, [stored(1)], { defer: true });
    expect(statuses()).toEqual([]);
    await reader.retry(ORG);
    expect(statuses()).toEqual([[1, 'verified']]);
  });

  it('drops a buffer of the previous generation when new entries arrive after a sign-out', async () => {
    await reader.ingest(ORG, SCOPE, [stored(1)], { defer: true });
    store.reset();
    await reader.ingest(ORG, SCOPE, [stored(2)], { defer: true });
    clock.advance(FRAME_MS);
    expect(statuses()).toEqual([[2, 'verified']]);
  });

  it('uses the system clock by default', async () => {
    vi.useFakeTimers();
    try {
      const live = new EventReader(store, { trigger: async () => true });
      vi.spyOn(live, 'read').mockImplementation(async (entry) => ({
        ...entry,
        status: 'verified',
        header: { id: `id-${entry.scopeSeq}` },
        body: {},
      }));
      await live.ingest(ORG, SCOPE, [stored(1)], { defer: true });
      expect(statuses()).toEqual([]);
      await vi.advanceTimersByTimeAsync(FRAME_MS);
      expect(statuses()).toEqual([[1, 'verified']]);
      await live.ingest(ORG, SCOPE, [stored(2)], { defer: true });
      live.flush();
      expect(statuses()).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('live bulk events no view shows (ADR-063)', () => {
  const live = (seq: number, kind: string, task: string | null, session: string | null): StoredEvent => ({
    scope_seq: seq,
    received_at: null,
    event: { header: { seq, kind, task, session } },
  });

  it('skips them without decrypting, notes the skip and keeps everything else', async () => {
    const close = show(store, { scope: SCOPE, task: 'shown' });
    const before = skipMark(store, { task: 'hidden', session: 's2' });
    await reader.ingest(
      ORG,
      SCOPE,
      [
        live(1, 'session.output', 'hidden', 's2'),
        live(2, 'session.output', 'shown', 's1'),
        live(3, 'task.meta', 'hidden', null),
        live(4, 'session.terminal', 'hidden', 's2'),
        { scope_seq: 5, received_at: null, event: 'raw' },
      ],
      { defer: true, skipUnshown: true },
    );
    clock.advance(0);
    expect(statuses()).toEqual([2, 3, 5].map((seq) => [seq, 'verified']));
    expect(reader.read).toHaveBeenCalledTimes(3);
    expect(skipMark(store, { task: 'hidden', session: 's2' })).not.toBe(before);
    // pulls (no `skipUnshown`) store bulk events of any task
    await reader.ingest(ORG, SCOPE, [live(6, 'session.output', 'hidden', 's2')]);
    expect(statuses().at(-1)).toEqual([6, 'verified']);
    close();
  });
});
